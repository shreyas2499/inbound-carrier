"""Shaping TMS records into agent-facing JSON."""
from __future__ import annotations


def public_load(load: dict) -> dict:
    """Agent-facing view of a load: MAX_BUY and the posted RATE both stripped (so
    the agent can neither pay above the hidden ceiling nor anchor on the loadboard
    sticker), internal markers dropped, datetimes rendered ISO for JSON-safety."""
    out = {}
    for key, value in load.items():
        if key in ("MAX_BUY", "RATE") or key.startswith("_"):
            continue
        out[key] = value.isoformat() if hasattr(value, "isoformat") else value
    return out
