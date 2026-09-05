"""Unit tests for the OTP store (issue / peek / verify)."""
import time

import pytest

from adapter import otp


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Point the store at a throwaway DB and reset its init/config per test."""
    monkeypatch.setattr(otp, "_DB_PATH", str(tmp_path / "otp.db"))
    monkeypatch.setattr(otp, "_initialized", False)
    monkeypatch.setattr(otp, "OTP_TTL", 180)
    monkeypatch.setattr(otp, "OTP_MAX_ATTEMPTS", 4)
    yield


def test_peek_before_issue_is_none(fresh_db):
    assert otp.peek("872144") == {"status": "none"}


def test_issue_hides_code_but_peek_reveals(fresh_db):
    r = otp.issue("MC 872144")  # non-digits stripped
    assert r["sent"] is True and r["mc_number"] == "872144"
    assert "code" not in r  # the agent must never receive the code
    p = otp.peek("872144")
    assert p["status"] == "active"
    assert len(p["code"]) == 6 and p["code"].isdigit()
    assert 0 < p["expires_in"] <= 180


def test_correct_code_verifies_and_is_single_use(fresh_db):
    otp.issue("872144")
    code = otp.peek("872144")["code"]
    assert otp.verify("872144", code) == {"verified": True, "reason": "ok"}
    # consumed: peek now reports verified, and re-verify stays true (idempotent)
    assert otp.peek("872144") == {"status": "verified", "verified": True}
    assert otp.verify("872144", code)["verified"] is True


def test_wrong_code_counts_down_then_locks(fresh_db):
    otp.issue("111111")
    real = otp.peek("111111")["code"]
    bad = "000000" if real != "000000" else "999999"
    remaining = [otp.verify("111111", bad)["attempts_remaining"] for _ in range(4)]
    assert remaining == [3, 2, 1, 0]
    assert otp.verify("111111", bad)["reason"] == "too_many_attempts"
    # once locked, even the correct code is refused
    assert otp.verify("111111", real)["reason"] == "too_many_attempts"


def test_verify_without_issue_is_rejected(fresh_db):
    assert otp.verify("555555", "123456") == {"verified": False, "reason": "no_code_issued"}


def test_expired_code_is_gone(fresh_db, monkeypatch):
    monkeypatch.setattr(otp, "OTP_TTL", 1)
    otp.issue("222222")
    time.sleep(1.1)
    assert otp.peek("222222") == {"status": "none"}
    assert otp.verify("222222", "123456")["reason"] == "expired"


def test_missing_inputs(fresh_db):
    assert otp.issue("")["sent"] is False
    assert otp.peek("") == {"status": "none"}
    assert otp.verify("", "123456")["verified"] is False
    otp.issue("333333")
    assert otp.verify("333333", "")["verified"] is False
