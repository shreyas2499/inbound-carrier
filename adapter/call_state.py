"""Per-call negotiation state that the ADAPTER owns.

WHY THIS EXISTS
    `call_records.rounds` must be the number of times a rate was actually put on
    the table. The workflow cannot supply that honestly: `evaluate_offer.round`
    is the AGENT's own count of itself, and there is a logged run where it
    skipped a rung (round 3 -> round 4 on an unchanged offer). The adapter sees
    every call to /tools/evaluate_offer, so it can just count them.

    SQLite for the same reason as otp.py: gunicorn runs several workers, and a
    process-local dict would only count the rounds that happened to land on one
    of them. The file lives in /tmp, so the count is per-container and resets on
    redeploy -- fine, because a count only has to survive one phone call.

    Keyed on (run_id, load_id): if a caller abandons one load and negotiates a
    different one in the same call, the second load starts at round 1.

Env knobs:
    CALL_STATE_DB_PATH   SQLite file path   (default /tmp/carrier_call_state.db)
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager

_DB_PATH = os.environ.get("CALL_STATE_DB_PATH", "/tmp/carrier_call_state.db")

_init_lock = threading.Lock()
_initialized = False


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
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        with _db() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS negotiation_rounds (
                    run_id   TEXT NOT NULL,
                    load_id  TEXT NOT NULL,
                    rounds   INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (run_id, load_id)
                )"""
            )
        _initialized = True


def bump_round(run_id, load_id) -> int:
    """Record one more rate exchange for this (call, load) and return the running
    total. Never raises: a counter must not be able to break a live negotiation,
    so on any storage error it returns 0 and the caller simply writes no count."""
    if not run_id or not load_id:
        return 0
    try:
        _ensure_schema()
        with _db() as conn:
            conn.execute(
                """INSERT INTO negotiation_rounds (run_id, load_id, rounds)
                   VALUES (?, ?, 1)
                   ON CONFLICT(run_id, load_id)
                   DO UPDATE SET rounds = rounds + 1""",
                (str(run_id), str(load_id)),
            )
            row = conn.execute(
                "SELECT rounds FROM negotiation_rounds WHERE run_id=? AND load_id=?",
                (str(run_id), str(load_id)),
            ).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def round_count(run_id, load_id) -> int:
    """Current total without incrementing. Never raises."""
    if not run_id or not load_id:
        return 0
    try:
        _ensure_schema()
        with _db() as conn:
            row = conn.execute(
                "SELECT rounds FROM negotiation_rounds WHERE run_id=? AND load_id=?",
                (str(run_id), str(load_id)),
            ).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0
