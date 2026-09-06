"""One-time-code issuance and verification for carrier identity.

STORAGE: Twin (`otp_challenges`), and nothing else. There is deliberately no
local copy -- one store, one source of truth, one thing that can fail.

Three consequences of that choice, all accepted knowingly:

  1. The code is stored in PLAINTEXT. /otp/peek has to read it back -- that
     endpoint IS the delivery channel, the demo's stand-in for an SMS -- so with
     no local store there is nowhere else for it to live. This is only defensible
     because /otp/peek is already public and unauthenticated: the code is
     readable by anyone who knows the MC for its 3-minute life, by design.
     A real deployment sends by SMS and stores only a digest.
  2. Identity verification now depends on Twin being reachable. It fails CLOSED:
     if Twin is down nobody gets verified, which is the right direction for a
     security gate but does couple the gate to the database's uptime.
  3. Twin has no server-side filter, so every read is a bounded client-side scan.
     Fine at one row per carrier; it is O(n) and would need a Twin View if this
     table ever grew.

Reads on the /otp/peek path are cached for a second or two, because the device
page polls every ~1.5s and each poll would otherwise be a fresh scan. That cache
is a cache -- it never decides anything, and verification never reads it.

Env knobs (all optional, sane defaults):
  OTP_TTL_SECONDS   how long an issued code stays valid   (default 180)
  OTP_MAX_ATTEMPTS  wrong-code guesses before it locks     (default 4)
  OTP_PEEK_CACHE_SECONDS  device-poll cache window         (default 2)
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from adapter.cache import TTLCache

OTP_TTL = int(os.environ.get("OTP_TTL_SECONDS", "180"))
OTP_MAX_ATTEMPTS = int(os.environ.get("OTP_MAX_ATTEMPTS", "4"))
_PEEK_TTL = float(os.environ.get("OTP_PEEK_CACHE_SECONDS", "2"))

_TABLE = "otp_challenges"
_peek_cache = TTLCache(ttl_seconds=_PEEK_TTL)
_log = logging.getLogger(__name__)

# The last store error, verbatim. The gate must not leak internals to the agent,
# so the tool response says only "store_unavailable" -- but swallowing the cause
# entirely makes a failure undiagnosable from the outside, which is how the first
# live OTP failure cost a whole test call. /debug/twin_probe reads this.
_last_error: str | None = None


def last_error():
    return _last_error


def _record(exc: Exception, where: str) -> None:
    global _last_error
    _last_error = f"{where}: {type(exc).__name__}: {exc}"[:500]
    _log.error("otp store failure -- %s", _last_error)


def _digits(value) -> str:
    return "".join(ch for ch in str(value) if ch.isdigit())


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _parse(value):
    """Timestamps come back from Twin as ISO strings. Returns None on anything
    unparseable so a malformed row reads as 'no live challenge' rather than
    throwing on the verification path."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _row(client, mc: str):
    return client.find_row(_TABLE, "mc_number", mc)


def issue(client, mc_number, run_id=None) -> dict:
    """Mint a fresh code for an MC, replacing any prior one. Returns only
    metadata -- never the code itself, so the agent cannot leak what it never
    saw."""
    mc = _digits(mc_number)
    if not mc:
        return {"sent": False, "reason": "no MC number provided"}
    if not getattr(client, "enabled", False):
        return {"sent": False, "reason": "store_unavailable"}

    now = _now()
    values = {
        "code": f"{secrets.randbelow(1000000):06d}",
        "created_at": _iso(now),
        "expires_at": _iso(now + timedelta(seconds=OTP_TTL)),
        "attempts": 0,
        "verified": False,
        "verified_at": None,
        "run_id": str(run_id) if run_id else None,
    }
    try:
        existing = _row(client, mc)
        if existing:
            client.update_row(_TABLE, {"id": existing["id"]}, values)
        else:
            client.insert_row(_TABLE, {"mc_number": mc, **values})
    except Exception as exc:
        # Never surface a store error as a code the carrier will wait for --
        # but do record it, or the failure is invisible from every angle.
        _record(exc, "issue")
        return {"sent": False, "reason": "store_unavailable"}

    _peek_cache.invalidate(mc)  # force the device's next poll to read through
    return {"sent": True, "mc_number": mc, "expires_in": OTP_TTL, "ttl": OTP_TTL}


def peek(client, mc_number) -> dict:
    """Public read used by the carrier device page. Reveals the active code --
    this IS the delivery channel in the demo. Never returns a code once expired."""
    mc = _digits(mc_number)
    if not mc:
        return {"status": "none"}

    row = _peek_cache.get(mc)
    if row is None:
        if not getattr(client, "enabled", False):
            return {"status": "none"}
        try:
            row = _row(client, mc)
        except Exception as exc:
            _record(exc, "peek")
            return {"status": "none"}
        if row:
            _peek_cache.set(mc, row)
    if not row:
        return {"status": "none"}

    expires = _parse(row.get("expires_at"))
    # Expiry first: a stale verified row must not keep showing "verified" forever.
    if not expires or _now() >= expires:
        return {"status": "none"}
    if row.get("verified"):
        # expires_in rides along on the verified response too, so the device page
        # can time its own panel out locally instead of depending on a poll
        # arriving to tell it the challenge is dead.
        return {"status": "verified", "verified": True,
                "expires_in": int(round((expires - _now()).total_seconds())),
                "ttl": OTP_TTL}
    return {"status": "active", "code": row.get("code"),
            "expires_in": int(round((expires - _now()).total_seconds())),
            "ttl": OTP_TTL}


def verify(client, mc_number, code) -> dict:
    """Agent-facing check. Enforces: a code must have been issued, not expired,
    under the attempt cap, and an exact match. On success the code is consumed
    (marked verified). This is the server-side half of the anti-social-engineering
    guarantee -- nothing here can be bypassed by conversation.

    Reads live, never from the peek cache: a gate does not get to be stale."""
    mc = _digits(mc_number)
    submitted = _digits(code)
    if not mc or not submitted:
        return {"verified": False, "reason": "missing_mc_or_code"}
    if not getattr(client, "enabled", False):
        return {"verified": False, "reason": "store_unavailable"}

    try:
        row = _row(client, mc)
    except Exception as exc:
        # Fails CLOSED. A store we cannot read is not permission to proceed.
        _record(exc, "verify.read")
        return {"verified": False, "reason": "store_unavailable"}
    if not row:
        return {"verified": False, "reason": "no_code_issued"}

    expires = _parse(row.get("expires_at"))
    attempts = int(row.get("attempts") or 0)
    # A code passes ONLY while it is live AND matches. An already-verified row
    # does NOT auto-pass a new (possibly wrong) code, and expired / attempt-locked
    # rows never pass. This is what stops a stale "verified" state -- or a skipped
    # send_otp -- from waving a dummy code through.
    if not expires or _now() >= expires:
        return {"verified": False, "reason": "expired"}
    if attempts >= OTP_MAX_ATTEMPTS:
        return {"verified": False, "reason": "too_many_attempts"}

    pk = {"id": row["id"]}
    if secrets.compare_digest(str(row.get("code") or ""), submitted):
        if not row.get("verified"):
            try:
                client.update_row(_TABLE, pk, {"verified": True,
                                               "verified_at": _iso(_now())})
            except Exception as exc:
                _record(exc, "verify.mark")  # the check passed; recording is best-effort
        _peek_cache.invalidate(mc)
        return {"verified": True, "reason": "ok"}

    try:
        client.update_row(_TABLE, pk, {"attempts": attempts + 1})
    except Exception as exc:
        _record(exc, "verify.attempts")
    _peek_cache.invalidate(mc)
    remaining = max(0, OTP_MAX_ATTEMPTS - (attempts + 1))
    return {"verified": False, "reason": "incorrect", "attempts_remaining": remaining}
