#!/usr/bin/env python3
"""Live smoke test against the real TMS. Run from a machine with network access
to the TMS (your Mac, not the sandbox):

    python scripts/smoke_tms.py

Reads creds from .env. Runs DEBUG_ECHO (proves auth + framing), then a small
LOAD_QUERY, then LOAD_GET on the first result — which also shows whether this
token is flagged for MAX_BUY and how the ceiling compares to the posted rate.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapter.config import Config  # noqa: E402


def main() -> int:
    cfg = Config.from_env()
    if not cfg.tms_host or not cfg.tms_token:
        print("Missing TMS_HOST / TMS_AUTH_TOKEN — is .env populated?")
        return 1
    client = cfg.make_client()

    print(f"1) DEBUG_ECHO -> {cfg.tms_host}:{cfg.tms_port}")
    echo = client.debug_echo("HELLO").records[0]
    print("   ", {k: echo[k] for k in echo})

    print("2) LOAD_QUERY  (GA -> TX, dry van)")
    loads = client.load_query(ORIG_STATE="GA", DEST_STATE="TX", EQTYPE="DRY_VAN", MAX_RESULTS=3)
    print(f"    {len(loads)} load(s) returned")
    if not loads:
        print("    (no loads on that lane right now — try another filter)")
        return 0

    load_id = loads[0]["LOAD_ID"]
    print(f"3) LOAD_GET   {load_id}")
    full = client.load_get(load_id)
    if "MAX_BUY" in full:
        rate, ceil = full.get("RATE"), full["MAX_BUY"]
        where = "below" if ceil < rate else "above"
        print(f"    RATE={rate}  MAX_BUY={ceil}  (ceiling is {where} the posted rate)")
    else:
        print("    MAX_BUY ABSENT — this token is not flagged for the ceiling")
    print("\nSmoke test complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
