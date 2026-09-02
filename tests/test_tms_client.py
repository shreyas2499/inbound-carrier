"""Resilience tests for the TMS socket client, driven by the scriptable mock.

Each of the four injected fault categories gets a test, plus the booking-safety
cases (never blind-retry, surface ambiguity).
"""
import time

import pytest

from adapter.tms_client import TmsClient, TmsUnavailable, BookAmbiguousError
from adapter.tms_codec import TmsError, FramingError
from mock_tms import MockTms

GOOD_GET = b"LOAD_ID:LD0000045821|ORIG_STATE:GA|RATE:0002150|STATUS:OPEN |MAX_BUY:0001950\r\nEND\r\n"
PARTIAL = b"LOAD_ID:LD0000045821|RATE:0002150"                 # prefix, no END
MALFORMED = b"LOAD_ID:LD1|GARBAGE|RATE:0002150\r\nEND\r\n"      # bad token mid-record
ERR_UNKNOWN = b"ERR|CODE:UNKNOWN_LOAD|MSG:load not found\r\n"
BOOK_OK = b"LOAD_ID:LD0000045821|BOOKING_REF:BR00000000091277|STATUS:BOOKED |TIMESTAMP:20260504193122\r\nEND\r\n"


def _client(host, port):
    # Short timeout + no backoff so the fault tests run fast.
    return TmsClient(host, port, "t-test", timeout=0.5, max_retries=2, backoff_base=0)


def _counting(responses):
    """Return (handler, calls) where handler replays `responses` by call index."""
    calls = []

    def handler(_request):
        i = len(calls)
        calls.append(1)
        return responses[min(i, len(responses) - 1)]

    return handler, calls


# --- happy path ------------------------------------------------------------

def test_get_happy_path_parses_record():
    with MockTms(lambda r: (GOOD_GET, 0)) as (h, p):
        rec = _client(h, p).load_get("LD0000045821")
    assert rec["RATE"] == 2150 and rec["MAX_BUY"] == 1950


# --- the four faults -------------------------------------------------------

def test_timeout_retries_then_raises():
    handler, calls = _counting([(b"", 1.5)])          # send nothing, hold
    with MockTms(handler) as (h, p):
        with pytest.raises(TmsUnavailable):
            _client(h, p).load_get("LD1")
    assert len(calls) == 3                             # 1 attempt + 2 retries

def test_partial_response_then_recovers_on_retry():
    handler, calls = _counting([(PARTIAL, 0), (GOOD_GET, 0)])
    with MockTms(handler) as (h, p):
        rec = _client(h, p).load_get("LD0000045821")
    assert rec["RATE"] == 2150
    assert len(calls) == 2                             # first partial, second good

def test_malformed_framing_retries_then_raises():
    handler, calls = _counting([(MALFORMED, 0)])
    with MockTms(handler) as (h, p):
        with pytest.raises(FramingError):
            _client(h, p).load_get("LD1")
    assert len(calls) == 3

def test_delayed_termination_returns_promptly():
    # Full response, then the server holds the socket open well past our timeout.
    with MockTms(lambda r: (GOOD_GET, 2.0)) as (h, p):
        start = time.time()
        rec = _client(h, p).load_get("LD0000045821")
        elapsed = time.time() - start
    assert rec["RATE"] == 2150
    assert elapsed < 0.5                               # did NOT wait for the hold/close


# --- structured errors are not faults --------------------------------------

def test_structured_error_is_not_retried():
    handler, calls = _counting([(ERR_UNKNOWN, 0)])
    with MockTms(handler) as (h, p):
        with pytest.raises(TmsError) as ei:
            _client(h, p).load_get("LD9999999999")
    assert ei.value.code == "UNKNOWN_LOAD"
    assert len(calls) == 1                             # a clean 'no' — no retry


# --- booking safety --------------------------------------------------------

def test_book_success_returns_booking_ref():
    with MockTms(lambda r: (BOOK_OK, 0)) as (h, p):
        rec = _client(h, p).load_book("LD0000045821", "872144", 2200)
    assert rec["BOOKING_REF"] == "BR00000000091277"
    assert rec["STATUS"] == "BOOKED"

def test_book_timeout_is_ambiguous_and_never_retried():
    handler, calls = _counting([(b"", 1.5)])           # book times out
    with MockTms(handler) as (h, p):
        with pytest.raises(BookAmbiguousError) as ei:
            _client(h, p).load_book("LD0000045821", "872144", 2200)
    assert ei.value.load_id == "LD0000045821"
    assert len(calls) == 1                             # the whole point: no blind retry
