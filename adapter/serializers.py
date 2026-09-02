"""Shaping TMS records into agent-facing JSON."""
from __future__ import annotations


def public_load(load: dict) -> dict:
    """Agent-facing view of a load: MAX_BUY and internal markers stripped,
    datetimes rendered as ISO strings so the payload is JSON-safe."""
    out = {}
    for key, value in load.items():
        if key == "MAX_BUY" or key.startswith("_"):
            continue
        out[key] = value.isoformat() if hasattr(value, "isoformat") else value
    return out
