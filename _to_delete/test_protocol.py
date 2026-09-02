"""Unit tests for the TMS protocol codec.

Every fixture below is a VERBATIM transcript from the HappyRobot Legacy TMS
Protocol Reference (doc HR-LTMS-PR-001). If the codec parses these, it parses
the real server.
"""
from datetime import datetime

import pytest

from adapter.protocol import (
    encode_request, parse_response, TmsError, FramingError, ProtocolError,
)

CRLF = "\r\n"


# --- encoder ---------------------------------------------------------------

def test_encode_puts_cmd_first_then_auth():
    parts = encode_request("LOAD_GET", "t-9c3a", load_id="LD0000045821").decode().rstrip(CRLF).split("|")
    assert parts[0] == "CMD:LOAD_GET"
    assert parts[1] == "AUTH:t-9c3a"
    assert "LOAD_ID:LD0000045821" in parts


def test_encode_rejects_delimiter_injection():
    with pytest.raises(ProtocolError):
        encode_request("LOAD_GET", "t", load_id="LD1|DROP")


def test_encode_requires_auth():
    with pytest.raises(ProtocolError):
        encode_request("DEBUG_ECHO", "")


def test_encode_omits_none_fields():
    line = encode_request("LOAD_QUERY", "t", orig_state="GA", dest_state=None).decode()
    assert "ORIG_STATE:GA" in line and "DEST_STATE" not in line


# --- DEBUG_ECHO (leading bare marker) --------------------------------------

def test_debug_echo_round_trip():
    resp = parse_response("ECHO|AUTH:OK|FIELDS_PARSED:3|MSG:HELLO" + CRLF + "END" + CRLF)
    rec = resp.records[0]
    assert rec["_MARKER"] == "ECHO"
    assert rec["AUTH"] == "OK"
    assert rec["FIELDS_PARSED"] == "3"
    assert rec["MSG"] == "HELLO"


def test_debug_echo_bad_auth():
    with pytest.raises(TmsError) as ei:
        parse_response("ERR|CODE:AUTH_FAILED|MSG:invalid or missing auth token" + CRLF)
    assert ei.value.code == "AUTH_FAILED"


# --- LOAD_GET --------------------------------------------------------------

def test_load_get_full_record_dry_van():
    raw = (
        "LOAD_ID:LD0000045821|ORIG_CITY:Atlanta |ORIG_STATE:GA|ORIG_ZIP:30303|"
        "DEST_CITY:Dallas |DEST_STATE:TX|DEST_ZIP:75201|PICKUP_DT:20260512080000|"
        "DELIVERY_DT:20260513170000|EQTYPE:DRY_VAN |RATE:0002150|WEIGHT:0042000|"
        "COMMODITY:PALLETIZED CONSUMER GOODS |PIECES:000026|MILES:000785|"
        "DIMS:48X40 STD GMA PALLETS |NOTES:Drop trailer at destination. Appt required. |"
        "STATUS:OPEN |MAX_BUY:0001950" + CRLF + "END" + CRLF
    )
    rec = parse_response(raw).records[0]
    assert rec["LOAD_ID"] == "LD0000045821"
    assert rec["ORIG_CITY"] == "Atlanta" and rec["ORIG_STATE"] == "GA"
    assert rec["EQTYPE"] == "DRY_VAN"
    assert rec["RATE"] == 2150 and rec["MAX_BUY"] == 1950
    assert rec["WEIGHT"] == 42000 and rec["PIECES"] == 26 and rec["MILES"] == 785
    assert rec["PICKUP_DT"] == datetime(2026, 5, 12, 8, 0, 0)
    assert rec["DELIVERY_DT"] == datetime(2026, 5, 13, 17, 0, 0)
    assert rec["STATUS"] == "OPEN"
    assert rec["NOTES"].startswith("Drop trailer")


def test_load_get_blank_notes_collapses_to_empty():
    raw = (
        "LOAD_ID:LD0000045903|ORIG_CITY:Atlanta |ORIG_STATE:GA|RATE:0002280|"
        "NOTES: |STATUS:OPEN |MAX_BUY:0002065" + CRLF + "END" + CRLF
    )
    rec = parse_response(raw).records[0]
    assert rec["NOTES"] == ""
    assert rec["MAX_BUY"] == 2065


def test_load_get_unknown_id():
    with pytest.raises(TmsError) as ei:
        parse_response("ERR|CODE:UNKNOWN_LOAD|MSG:load not found" + CRLF)
    assert ei.value.code == "UNKNOWN_LOAD"


# --- LOAD_QUERY ------------------------------------------------------------

def test_load_query_multi_record_has_no_max_buy():
    raw = (
        "LOAD_ID:LD0000045821|ORIG_CITY:Atlanta |ORIG_STATE:GA|DEST_STATE:TX|"
        "EQTYPE:DRY_VAN |RATE:0002150|MILES:000785|STATUS:OPEN" + CRLF +
        "LOAD_ID:LD0000045903|ORIG_CITY:Atlanta |ORIG_STATE:GA|DEST_STATE:TX|"
        "EQTYPE:DRY_VAN |RATE:0002280|MILES:000789|STATUS:OPEN" + CRLF + "END" + CRLF
    )
    resp = parse_response(raw)
    assert [r["LOAD_ID"] for r in resp.records] == ["LD0000045821", "LD0000045903"]
    assert all("MAX_BUY" not in r for r in resp.records)  # ceiling never in query


def test_load_query_empty_result():
    assert parse_response("END" + CRLF).records == []


def test_load_query_missing_filter():
    with pytest.raises(TmsError) as ei:
        parse_response("ERR|CODE:MISSING_FIELD|MSG:at least one filter required" + CRLF)
    assert ei.value.code == "MISSING_FIELD"


# --- LOAD_BOOK -------------------------------------------------------------

def test_load_book_success():
    raw = (
        "LOAD_ID:LD0000045821|BOOKING_REF:BR00000000091277|STATUS:BOOKED |"
        "TIMESTAMP:20260504193122" + CRLF + "END" + CRLF
    )
    rec = parse_response(raw).records[0]
    assert rec["BOOKING_REF"] == "BR00000000091277"   # opaque, kept as string
    assert rec["STATUS"] == "BOOKED"
    assert rec["TIMESTAMP"] == datetime(2026, 5, 4, 19, 31, 22)


def test_load_book_already_booked_is_sticky():
    with pytest.raises(TmsError) as ei:
        parse_response("ERR|CODE:ALREADY_BOOKED|MSG:load not available" + CRLF)
    assert ei.value.code == "ALREADY_BOOKED"


def test_load_book_invalid_rate():
    with pytest.raises(TmsError) as ei:
        parse_response("ERR|CODE:INVALID_RATE|MSG:rate rejected" + CRLF)
    assert ei.value.code == "INVALID_RATE"


# --- fault detection on the wire -------------------------------------------

def test_partial_response_missing_end_is_framing_error():
    # Partial fault: a prefix of a valid record, no END terminator.
    with pytest.raises(FramingError):
        parse_response("LOAD_ID:LD0000045821|RATE:0002150" + CRLF)


def test_malformed_midline_token_is_framing_error():
    # Malformed fault: an extra delimiter / colon-less token mid-record.
    with pytest.raises(FramingError):
        parse_response("LOAD_ID:LD1|GARBAGE|RATE:0002150" + CRLF + "END" + CRLF)


def test_empty_response_is_framing_error():
    with pytest.raises(FramingError):
        parse_response("")
