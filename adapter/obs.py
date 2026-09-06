"""Request correlation + structured tool logging.

Every /tools/* call MAY carry two optional fields in its JSON body:

    run_id       the HappyRobot workflow RUN id -- ties adapter activity to one call.
                 Aliases call_id / callId are accepted on input; the field is
                 always EMITTED as run_id, matching call_records.run_id and
                 event_log.run_id so the three join without translation.
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

# Body fields the workflow sends purely for correlation. They are NOT tool
# arguments. Any endpoint that forwards its body onward MUST strip these first --
# search_loads builds TMS query filters out of the raw body, so without this a
# run id would be sent to the legacy TMS as a load filter named RUN_ID.
CORRELATION_KEYS = frozenset({"run_id", "call_id", "callid", "environment", "env"})


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
        "run_id": _clean(body.get("run_id") or body.get("call_id") or body.get("callId")),
        "environment": _clean(body.get("environment") or body.get("env")),
    }


def _status_label(response) -> str:
    """Map an HTTP response to the short status vocabulary event_log stores."""
    if response.status_code < 400:
        return "ok"
    try:
        payload = response.get_json(silent=True) or {}
    except Exception:
        payload = {}
    return str(payload.get("error") or f"http_{response.status_code}")


def register_observability(app) -> None:
    """Time every tool call, log it as one JSON line, and mirror it into Twin's
    `event_log` table.

    ONE wiring point on purpose. The alternative -- a log_event() call inside each
    route -- is six call sites that must each remember to fire on the error paths
    too, and the first one anybody forgets leaves a silent hole in the audit trail.

    What gets stored as `response` is the ADAPTER's response, not the raw upstream
    record. That is a deliberate narrowing of what twin_models.EventLog originally
    described: the adapter's response has already had MAX_BUY stripped by the
    serializer, so no ceiling is ever written to Twin in any form. The raw upstream
    payload stays available in the container logs and via /debug/* when a specific
    TMS reply needs inspecting."""

    @app.before_request
    def _start_timer():
        g._t0 = time.perf_counter()

    @app.after_request
    def _log_tool_call(response):
        if request.path.startswith(_OBSERVED_PREFIXES):
            started = getattr(g, "_t0", None)
            latency_ms = int((time.perf_counter() - started) * 1000) if started else None
            # request.get_json is cached by Flask, so this re-read is cheap.
            body = request.get_json(silent=True)
            ctx = call_context(body)
            tool = request.path.lstrip("/")
            app.logger.info(json.dumps({
                "tool": tool,
                "status": response.status_code,
                "latency_ms": latency_ms,
                **ctx,
            }, separators=(",", ":")))

            # Fire-and-forget, and no-ops entirely until HAPPYROBOT_API_KEY is set.
            # Wrapped because observability must never be able to fail a tool call.
            try:
                from adapter import twin_helper
                payload = body if isinstance(body, dict) else {}
                twin_helper.log_event(
                    app.config.get("TWIN_CLIENT"),
                    tool=tool,
                    status=_status_label(response),
                    request=payload,
                    response=response.get_json(silent=True),
                    run_id=ctx.get("run_id"),
                    environment=ctx.get("environment"),
                    mc_number=_clean(payload.get("mc_number")),
                    load_id=_clean(payload.get("load_id")),
                    latency_ms=latency_ms,
                )
            except Exception:
                pass
        return response
