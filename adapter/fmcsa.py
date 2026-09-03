"""FMCSA carrier-authority lookup.

Proxies the public FMCSA QCMobile API so (a) the FMCSA webKey stays server-side,
and (b) every failure mode — carrier not found, no active authority, out of
service, API slow/down — is handled here in Python and returned as a clean,
predictable shape the workflow can branch on, instead of pushing that logic into
the agent.

Endpoint used:  GET /carriers/docket-number/{mc}?webKey=...
Field names are transcribed from the FMCSA QCMobile docs; parsing is defensive
because the exact wrapper shape isn't guaranteed. Verify against a live response.
"""
from __future__ import annotations

import requests

FMCSA_BASE = "https://mobile.fmcsa.dot.gov/qc/services/carriers"


class FmcsaUnavailable(Exception):
    """FMCSA API was unreachable or returned an unusable response."""


def _digits(value) -> str:
    return "".join(ch for ch in str(value) if ch.isdigit())


def _carrier_record(data: dict) -> dict | None:
    content = data.get("content")
    if not content:
        return None
    rec = content[0] if isinstance(content, list) else content
    if isinstance(rec, dict) and "carrier" in rec:
        rec = rec["carrier"]
    return rec if isinstance(rec, dict) else None


def verify_mc(mc_number, web_key: str, *, timeout: float = 8.0) -> dict:
    """Look up a carrier by MC (docket) number; return an eligibility summary.

    Always returns a dict (never raises) for found / not-found / not-eligible —
    only a genuine API failure raises FmcsaUnavailable, so the caller can map that
    to a 'bear with me' retry rather than a decline.
    """
    mc = _digits(mc_number)
    if not mc:
        return {"found": False, "eligible": False, "mc_number": str(mc_number),
                "reason": "no MC number provided"}

    try:
        resp = requests.get(f"{FMCSA_BASE}/docket-number/{mc}",
                            params={"webKey": web_key}, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        raise FmcsaUnavailable(f"FMCSA lookup failed: {e}") from e
    except ValueError as e:
        raise FmcsaUnavailable(f"FMCSA returned non-JSON: {e}") from e

    carrier = _carrier_record(data)
    if not carrier:
        return {"found": False, "eligible": False, "mc_number": mc,
                "reason": "carrier not found in FMCSA"}

    allowed = str(carrier.get("allowedToOperate",
                              carrier.get("allowToOperate", ""))).upper() == "Y"
    out_of_service = str(carrier.get("outOfService", "")).upper() == "Y"
    return {
        "found": True,
        "eligible": allowed and not out_of_service,
        "mc_number": mc,
        "dot_number": carrier.get("dotNumber"),
        "legal_name": carrier.get("legalName") or carrier.get("dbaName"),
        "allowed_to_operate": allowed,
        "out_of_service": out_of_service,
        "phone": carrier.get("telephone") or carrier.get("phone"),
    }
