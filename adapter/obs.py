"""Request correlation + structured tool logging.

Every /tools/* call MAY carry two optional fields in its JSON body:

    call_id      the HappyRobot workflow RUN id -- ties adapter activity to one call
    environment  the workflow's `Execution Environment` global
                 (development | staging | production)

Both are OPTIONAL and purely observational. The adapter behaves identically with
or without them, and no endpoint reads or branches on them -- `environment` in
particular must NEVER weaken a gate (no "skip OTP in dev"); it exists to
partition data, nothing else.

When present they are emitted on every tool log line, so adapter logs can be
correlated back to a single run today. They are also exactly the two fields that
will be written into the Twin `event_log` table (see twin_models.EventLog) once
that table exists -- at which point this module's log sink is swapped for a
fire-and-forget Twin write, with no change to the workflow wiring.
"""
from __future__ import annotations

import json
import time

from flask import g, request

# /otp/peek is deliberately excluded: the carrier device polls it every ~1.5s and
# logging that would drown the useful lines.
_OBSERVED_PREFIXES = ("/tools/", "/debug/")


def _clean(value):
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text[:120] or None


def call_context(body=None) -> dict:
    """Pull the correlation fields out of a request body, accepting a few aliases
    so the workflow can name the chip either way."""
    body = body if isinstance(body, dict) else {}
    return {
        "call_id": _clean(body.get("call_id") or body.get("run_id") or body.get("callId")),
        "environment": _clean(body.get("environment") or body.get("env")),
    }


def register_observability(app) -> None:
    """Time every tool call and log it as one JSON line with its correlation ids."""

    @app.before_request
    def _start_timer():
        g._t0 = time.perf_counter()

    @app.after_request
    def _log_tool_call(response):
        if request.path.startswith(_OBSERVED_PREFIXES):
            started = getattr(g, "_t0", None)
            app.logger.info(json.dumps({
                "tool": request.path.lstrip("/"),
                "status": response.status_code,
                "latency_ms": int((time.perf_counter() - started) * 1000) if started else None,
                # request.get_json is cached by Flask, so this re-read is cheap.
                **call_context(request.get_json(silent=True)),
            }, separators=(",", ":")))
        return response
