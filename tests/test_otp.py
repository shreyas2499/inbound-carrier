"""The OTP gate itself, exercised directly against its store.

otp.py now talks to Twin rather than a local SQLite file, so these use a small
in-memory stand-in for the store. The behaviour under test is unchanged and is
the security-critical part: a code must be issued, live, under the attempt cap,
and an exact match -- nothing conversational can substitute for any of those.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from adapter import otp

MC = "872144"


class MemoryStore:
    enabled = True

    def __init__(self):
        self.rows = []
        self._n = 0

    def insert_row(self, table, values):
        self._n += 1
        row = {"id": f"r{self._n}", **values}
        self.rows.append(row)
        return row

    def update_row(self, table, primary_key, updates):
        for row in self.rows:
            if all(row.get(k) == v for k, v in primary_key.items()):
                row.update(updates)
                return row
        return None

    def find_row(self, table, column, value, **kw):
        return next((r for r in self.rows if str(r.get(column)) == str(value)), None)


@pytest.fixture
def store():
    otp._peek_cache._store.clear()
    return MemoryStore()


def _code(store):
    return store.rows[0]["code"]


def _expire(store):
    """Age the challenge past its TTL without waiting for it."""
    store.rows[0]["expires_at"] = (
        datetime.now(tz=timezone.utc) - timedelta(seconds=1)).isoformat()
    otp._peek_cache._store.clear()


def test_peek_before_any_code_is_issued(store):
    assert otp.peek(store, MC) == {"status": "none"}


def test_issue_returns_metadata_but_never_the_code(store):
    result = otp.issue(store, MC, run_id="run-1")
    assert result["sent"] is True
    assert "code" not in result
    assert store.rows[0]["run_id"] == "run-1"


def test_the_device_can_read_the_code_but_the_agent_cannot(store):
    otp.issue(store, MC)
    seen = otp.peek(store, MC)
    assert seen["status"] == "active"
    assert seen["code"] == _code(store)
    assert 0 < seen["expires_in"] <= otp.OTP_TTL


def test_correct_code_verifies_once_and_stays_verified_for_peek(store):
    otp.issue(store, MC)
    assert otp.verify(store, MC, _code(store)) == {"verified": True, "reason": "ok"}
    assert otp.peek(store, MC)["status"] == "verified"


def test_a_verified_row_does_not_wave_through_a_later_wrong_code(store):
    """The bug that let a dummy code pass: an already-verified challenge must not
    short-circuit a fresh check."""
    otp.issue(store, MC)
    otp.verify(store, MC, _code(store))
    assert otp.verify(store, MC, "000000")["verified"] is False


def test_wrong_codes_count_down_then_lock(store):
    otp.issue(store, MC)
    for expected in (3, 2, 1, 0):
        result = otp.verify(store, MC, "000000")
        assert result["verified"] is False
        assert result["attempts_remaining"] == expected
    assert otp.verify(store, MC, "000000")["reason"] == "too_many_attempts"


def test_the_locked_out_carrier_cannot_then_use_the_real_code(store):
    otp.issue(store, MC)
    real = _code(store)
    for _ in range(otp.OTP_MAX_ATTEMPTS):
        otp.verify(store, MC, "000000")
    assert otp.verify(store, MC, real)["reason"] == "too_many_attempts"


def test_verify_without_an_issued_code(store):
    assert otp.verify(store, MC, "123456") == {"verified": False,
                                               "reason": "no_code_issued"}


def test_expired_code_is_rejected_and_hidden(store):
    otp.issue(store, MC)
    real = _code(store)
    _expire(store)
    assert otp.verify(store, MC, real) == {"verified": False, "reason": "expired"}
    assert otp.peek(store, MC) == {"status": "none"}, "an expired code must not display"


def test_expired_verified_row_stops_showing_as_verified(store):
    otp.issue(store, MC)
    otp.verify(store, MC, _code(store))
    _expire(store)
    assert otp.peek(store, MC) == {"status": "none"}


def test_a_resend_supersedes_the_old_code_and_resets_attempts(store):
    otp.issue(store, MC)
    first = _code(store)
    otp.verify(store, MC, "000000")
    otp.issue(store, MC)
    assert len(store.rows) == 1, "one live challenge per carrier"
    assert store.rows[0]["attempts"] == 0
    assert otp.verify(store, MC, first)["verified"] is False


def test_missing_inputs(store):
    assert otp.issue(store, "")["sent"] is False
    assert otp.verify(store, "", "123456")["reason"] == "missing_mc_or_code"
    assert otp.verify(store, MC, "")["reason"] == "missing_mc_or_code"
    assert otp.peek(store, "") == {"status": "none"}


def test_the_gate_fails_closed_when_the_store_is_down(store):
    """Single-store design: an unreachable store must deny, never grant."""
    otp.issue(store, MC)

    class Broken(MemoryStore):
        def find_row(self, *a, **k): raise RuntimeError("twin down")

    otp._peek_cache._store.clear()
    assert otp.verify(Broken(), MC, "123456")["reason"] == "store_unavailable"


def test_a_disabled_store_issues_nothing(store):
    class Off(MemoryStore):
        enabled = False

    assert otp.issue(Off(), MC)["reason"] == "store_unavailable"
    assert otp.verify(Off(), MC, "123456")["reason"] == "store_unavailable"
