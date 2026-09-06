"""One-time-code issuance and verification for carrier identity.

Backed by a tiny SQLite table so state is shared across the gunicorn workers in
a single container -- a plain in-memory dict would live in one worker only, and a
code issued by worker A could not be verified by worker B. Codes are random,
single-use, attempt-limited and time-boxed. The agent-facing endpoints never
return a code; only the public /otp/peek (the carrier "device") reveals it, which
is the demo stand-in for an SMS arriving on the carrier's handset.

Env knobs (all optional, sane defaults):
  OTP_TTL_SECONDS   how long an issued code stays valid          (default 180)
  OTP_MAX_ATTEMPTS  wrong-code guesses before the code locks      (default 4)
  OTP_DB_PATH       SQLite file path                              (default /tmp/carrier_otp.db)
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from contextlib import contextmanager

OTP_TTL = int(os.environ.get("OTP_TTL_SECONDS", "180"))
OTP_MAX_ATTEMPTS = int(os.environ.get("OTP_MAX_ATTEMPTS", "4"))
_DB_PATH = os.environ.get("OTP_DB_PATH", "/tmp/carrier_otp.db")

_init_lock = threading.Lock()
_initialized = False


def _digits(value) -> str:
    return "".join(ch for ch in str(value) if ch.isdigit())


@contextmanager
def _db():
    conn = sqlite3.connect(_DB_PATH, timeout=5)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_schema() -> None:
    """Create the table once per process. Cheap idempotent guard so callers don't
    have to think about init ordering across workers."""
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        with _db() as conn:
            conn.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer
            conn.execute(
                """CREATE TABLE IF NOT EXISTS otp (
                    mc        TEXT PRIMARY KEY,
                    code      TEXT    NOT NULL,
                    created   REAL    NOT NULL,
                    expires   REAL    NOT NULL,
                    attempts  INTEGER NOT NULL DEFAULT 0,
                    verified  INTEGER NOT NULL DEFAULT 0
                )"""
            )
            # Added after the first cut; the store lives in /tmp so a plain
            # ALTER-if-missing is enough -- there is never old data to migrate.
            existing = {row[1] for row in conn.execute("PRAGMA table_info(otp)")}
            for column, ddl in (
                ("code_salt", "ALTER TABLE otp ADD COLUMN code_salt TEXT"),
                ("code_hash", "ALTER TABLE otp ADD COLUMN code_hash TEXT"),
                ("verified_at", "ALTER TABLE otp ADD COLUMN verified_at REAL"),
                ("run_id", "ALTER TABLE otp ADD COLUMN run_id TEXT"),
            ):
                if column not in existing:
                    conn.execute(ddl)
        _initialized = True


def _hash(code: str, salt: str) -> str:
    """Salted SHA-256 of a code, for the durable copy in Twin.

    Worth being honest about what this does and does not buy: the code space is
    only 10^6, so anyone holding the hash AND the salt can brute-force it in
    milliseconds. The hash stops a live secret sitting in plaintext in a shared
    database; what actually protects the gate is the 3-minute TTL and the
    attempt cap, both enforced here."""
    return hashlib.sha256(f"{salt}:{code}".encode()).hexdigest()


def issue(mc_number, run_id=None) -> dict:
    """Mint a fresh code for an MC, replacing any prior one. Returns only metadata
    -- never the code itself (the agent must not see it)."""
    _ensure_schema()
    mc = _digits(mc_number)
    if not mc:
        return {"sent": False, "reason": "no MC number provided"}
    code = f"{secrets.randbelow(1000000):06d}"
    salt = secrets.token_hex(8)
    now = time.time()
    with _db() as conn:
        conn.execute(
            """INSERT INTO otp (mc, code, created, expires, attempts, verified,
                                code_salt, code_hash, verified_at, run_id)
               VALUES (?, ?, ?, ?, 0, 0, ?, ?, NULL, ?)
               ON CONFLICT(mc) DO UPDATE SET
                   code=excluded.code, created=excluded.created,
                   expires=excluded.expires, attempts=0, verified=0,
                   code_salt=excluded.code_salt, code_hash=excluded.code_hash,
                   verified_at=NULL, run_id=excluded.run_id""",
            (mc, code, now, now + OTP_TTL, salt, _hash(code, salt),
             str(run_id) if run_id else None),
        )
    return {"sent": True, "mc_number": mc, "expires_in": OTP_TTL, "ttl": OTP_TTL}


def peek(mc_number) -> dict:
    """Public read used by the carrier device page. Reveals the active code (this
    IS the delivery channel in the demo). Never returns a code once expired."""
    _ensure_schema()
    mc = _digits(mc_number)
    if not mc:
        return {"status": "none"}
    now = time.time()
    with _db() as conn:
        row = conn.execute(
            "SELECT code, expires, verified FROM otp WHERE mc=?", (mc,)
        ).fetchone()
    if not row:
        return {"status": "none"}
    code, expires, verified = row
    # Expiry first: a stale verified row must not keep showing "verified" forever.
    if now >= expires:
        return {"status": "none"}
    if verified:
        return {"status": "verified", "verified": True}
    return {"status": "active", "code": code,
            "expires_in": int(round(expires - now)), "ttl": OTP_TTL}


def verify(mc_number, code) -> dict:
    """Agent-facing check. Enforces: a code must have been issued, not expired,
    under the attempt cap, and an exact match. On success the code is consumed
    (marked verified). This is the server-side half of the anti-social-engineering
    guarantee -- the agent cannot 'skip' it, because nothing here can be bypassed
    by conversation."""
    _ensure_schema()
    mc = _digits(mc_number)
    submitted = _digits(code)
    if not mc or not submitted:
        return {"verified": False, "reason": "missing_mc_or_code"}
    now = time.time()
    with _db() as conn:
        row = conn.execute(
            "SELECT code, expires, attempts, verified FROM otp WHERE mc=?", (mc,)
        ).fetchone()
        if not row:
            return {"verified": False, "reason": "no_code_issued"}
        real, expires, attempts, verified = row
        # A code passes ONLY while it is live AND matches. An already-verified row
        # does NOT auto-pass a new (possibly wrong) code, and expired / attempt-
        # locked rows never pass. This is what stops a stale "verified" state -- or
        # a skipped send_otp -- from waving a dummy code through.
        if now >= expires:
            return {"verified": False, "reason": "expired"}
        if attempts >= OTP_MAX_ATTEMPTS:
            return {"verified": False, "reason": "too_many_attempts"}
        if secrets.compare_digest(str(real), submitted):
            if not verified:
                conn.execute("UPDATE otp SET verified=1, verified_at=? WHERE mc=?",
                             (now, mc))
            return {"verified": True, "reason": "ok"}
        conn.execute("UPDATE otp SET attempts=attempts+1 WHERE mc=?", (mc,))
        remaining = max(0, OTP_MAX_ATTEMPTS - (attempts + 1))
        return {"verified": False, "reason": "incorrect", "attempts_remaining": remaining}


def snapshot(mc_number) -> dict | None:
    """The audit view of a carrier's current challenge -- everything EXCEPT the
    code. This is what gets mirrored into Twin's `otp_challenges`; the plaintext
    stays in this process-local store because it is the demo's delivery channel
    (the device page reads it back), and a live code has no business sitting in a
    shared database."""
    _ensure_schema()
    mc = _digits(mc_number)
    if not mc:
        return None
    with _db() as conn:
        row = conn.execute(
            """SELECT code_hash, created, expires, attempts, verified, verified_at, run_id
               FROM otp WHERE mc=?""", (mc,)
        ).fetchone()
    if not row:
        return None
    code_hash, created, expires, attempts, verified, verified_at, run_id = row
    return {
        "mc_number": mc,
        "code_hash": code_hash,
        "run_id": run_id,
        "created_at": _iso(created),
        "expires_at": _iso(expires),
        "attempts": int(attempts or 0),
        "verified": bool(verified),
        "verified_at": _iso(verified_at),
    }


def _iso(epoch):
    if epoch in (None, ""):
        return None
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()
