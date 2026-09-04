"""Twin (HappyRobot managed Postgres) logging helpers.

WHY THIS EXISTS
    The adapter owns the *raw* upstream traffic (FMCSA + TMS), so it is the
    natural writer for two audit-style tables:

        event_log  -> one row per API/tool call (verbatim request + response)
        carriers   -> one master row per carrier, upserted on mc_number

    The workflow separately writes `call_records` (the dashboard-facing summary).
    See adapter/twin_models.py for the full column reference.

DESIGN RULES (all deliberate)
    1. NON-BLOCKING / FIRE-AND-FORGET. Logging to Twin must NEVER break or slow a
       carrier call. Every public helper swallows its own errors (returns a bool
       / None) and, by default, runs the HTTP call on a daemon thread so the tool
       endpoint returns immediately. A Twin outage is invisible to the caller.
    2. OFF BY DEFAULT UNTIL CREDS EXIST. If HAPPYROBOT_API_KEY is blank the helpers
       short-circuit to a no-op. So this module is safe to wire into routes.py
       today and simply does nothing until you drop the key into .env.
    3. NEVER LOG SECRETS. `log_event` scrubs auth tokens / webKeys from the request
       payload defensively, even though callers are expected to pass sanitized
       dicts.
    4. max_buy IS INTERNAL. event_log intentionally stores the full raw response
       (which for get_load includes MAX_BUY). This table is an internal audit log
       and must never be surfaced to the agent or a carrier-facing app.

TWIN REST SURFACE (confirmed from docs, base = https://platform.happyrobot.ai/api/v2)
    GET    /twin/tables/{table}                read rows      (?limit=&offset=)   -> {rows,total}
    POST   /twin/tables/{table}/rows           insert         {"values": {...}}   -> 201
    PATCH  /twin/tables/{table}/rows           update         {"primaryKey":{...},"updates":{...}}
    DELETE /twin/tables/{table}/rows           delete         {"rowKeys":[{...}]}  -> {deletedCount}
    (there is NO server-side column filter on read — paging only.)

NOTE: nothing here runs automatically. It is imported and called explicitly from
routes.py once you decide to turn logging on.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)

# request-payload keys we refuse to persist, whatever the caller passes
_SECRET_KEYS = {
    "auth", "auth_token", "token", "tms_token", "tms_auth_token",
    "webkey", "web_key", "api_key", "apikey", "fmcsa_api_key", "authorization",
}


class TwinError(Exception):
    """A Twin REST call returned a non-2xx or could not be reached."""


# ---------------------------------------------------------------------------
# low-level REST primitives (raise TwinError; used by the guarded helpers below)
# ---------------------------------------------------------------------------
class TwinClient:
    """Thin wrapper over the Twin REST endpoints. One instance per app, created
    from Config. Stateless apart from the base URL + bearer token."""

    def __init__(self, api_key: str, api_base: str, *, timeout: float = 6.0):
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        """False until a key is configured — the whole module no-ops when off."""
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, *, json_body: Any = None,
                 params: Optional[dict] = None) -> Any:
        url = f"{self.api_base}{path}"
        try:
            resp = requests.request(
                method, url, headers=self._headers(),
                json=json_body, params=params, timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise TwinError(f"Twin {method} {path} unreachable: {exc}") from exc
        if not resp.ok:
            raise TwinError(f"Twin {method} {path} -> {resp.status_code}: {resp.text[:300]}")
        if resp.content:
            try:
                return resp.json()
            except ValueError:
                return resp.text
        return None

    # --- table-row primitives ---------------------------------------------
    def insert_row(self, table: str, values: dict[str, Any]) -> Any:
        """POST /twin/tables/{table}/rows  ->  201."""
        return self._request("POST", f"/twin/tables/{table}/rows",
                             json_body={"values": values})

    def update_row(self, table: str, primary_key: dict[str, Any],
                   updates: dict[str, Any]) -> Any:
        """PATCH /twin/tables/{table}/rows."""
        return self._request("PATCH", f"/twin/tables/{table}/rows",
                             json_body={"primaryKey": primary_key, "updates": updates})

    def delete_rows(self, table: str, row_keys: list[dict[str, Any]]) -> Any:
        """DELETE /twin/tables/{table}/rows  ->  {"deletedCount": n}."""
        return self._request("DELETE", f"/twin/tables/{table}/rows",
                             json_body={"rowKeys": row_keys})

    def get_rows(self, table: str, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """GET /twin/tables/{table}  ->  {rows, total}. Paging only, no filter."""
        return self._request("GET", f"/twin/tables/{table}",
                            params={"limit": limit, "offset": offset}) or {}

    def find_row(self, table: str, column: str, value: Any,
                 *, page: int = 200, max_scan: int = 2000) -> Optional[dict[str, Any]]:
        """Client-side lookup: page through the table and return the first row
        whose `column == value` (Twin has no server-side filter). Bounded by
        max_scan so a huge table can't turn one upsert into a full scan."""
        offset = 0
        while offset < max_scan:
            data = self.get_rows(table, limit=page, offset=offset)
            rows = data.get("rows", []) or []
            for row in rows:
                if str(row.get(column)) == str(value):
                    return row
            if len(rows) < page:  # last page reached
                break
            offset += page
        return None


# ---------------------------------------------------------------------------
# fire-and-forget dispatch: run a callable on a daemon thread, never propagate
# ---------------------------------------------------------------------------
def _safe_call(fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except TwinError as exc:
        log.warning("twin write skipped (non-fatal): %s", exc)
    except Exception as exc:  # never let logging crash a tool call
        log.warning("twin write unexpected error (non-fatal): %s", exc)


def _dispatch(fn, *args, background: bool = True, **kwargs) -> None:
    if background:
        threading.Thread(target=_safe_call, args=(fn, *args),
                         kwargs=kwargs, daemon=True).start()
    else:
        _safe_call(fn, *args, **kwargs)


def _scrub(payload: Any) -> Any:
    """Recursively drop secret-looking keys from a request payload."""
    if isinstance(payload, dict):
        return {k: _scrub(v) for k, v in payload.items()
                if k.lower() not in _SECRET_KEYS}
    if isinstance(payload, list):
        return [_scrub(v) for v in payload]
    return payload


# ---------------------------------------------------------------------------
# high-level, guarded helpers — THIS is what routes.py calls
# ---------------------------------------------------------------------------
def log_event(client: TwinClient, *, tool: str, status: str,
              request: Optional[dict] = None, response: Optional[Any] = None,
              call_id: Optional[str] = None, mc_number: Optional[str] = None,
              load_id: Optional[str] = None, latency_ms: Optional[int] = None,
              background: bool = True) -> None:
    """Append one row to `event_log`. Fire-and-forget: returns immediately, errors
    swallowed. Secrets are scrubbed from `request` before it is stored.

    `response` is stored verbatim (jsonb) — for get_load this DOES include
    MAX_BUY. event_log is internal audit only; never surface it to a carrier."""
    if not client.enabled:
        return
    values = {
        "tool": tool,
        "status": status,
        "call_id": call_id,
        "mc_number": mc_number,
        "load_id": load_id,
        "request": _scrub(request or {}),
        "response": response,
        "latency_ms": latency_ms,
    }
    _dispatch(client.insert_row, "event_log", values, background=background)


def upsert_carrier(client: TwinClient, *, mc_number: str,
                   dot_number: Optional[str] = None, legal_name: Optional[str] = None,
                   authority_eligible: Optional[bool] = None, phone: Optional[str] = None,
                   fmcsa_raw: Optional[dict] = None, background: bool = True) -> None:
    """Insert or update the master `carriers` row keyed on mc_number.

    Twin has no server-side filter, so this reads the table to find an existing
    row (find_row), then PATCHes it (bumping last_seen + call_count) or POSTs a
    new one. Fire-and-forget and fully guarded. For the low carrier volumes of a
    POC the bounded client-side scan is fine; if this table ever grows large,
    swap the find for a Twin View keyed on mc_number."""
    if not client.enabled:
        return

    def _do() -> None:
        existing = client.find_row("carriers", "mc_number", mc_number)
        fields = {
            "dot_number": dot_number,
            "legal_name": legal_name,
            "authority_eligible": authority_eligible,
            "phone": phone,
            "fmcsa_raw": fmcsa_raw,
        }
        fields = {k: v for k, v in fields.items() if v is not None}
        if existing:
            pk = {"id": existing["id"]}
            updates = dict(fields)
            updates["last_seen"] = "now()"  # server default expr; adjust if Twin wants ISO
            updates["call_count"] = int(existing.get("call_count") or 0) + 1
            client.update_row("carriers", pk, updates)
        else:
            values = {"mc_number": mc_number, "call_count": 1, **fields}
            client.insert_row("carriers", values)

    _dispatch(_do, background=background)


# ---------------------------------------------------------------------------
# app wiring helper
# ---------------------------------------------------------------------------
def client_from_config(config) -> TwinClient:
    """Build a TwinClient from adapter Config. Stored on the Flask app in app.py
    as app.config['TWIN_CLIENT'] so routes can grab it via current_app."""
    return TwinClient(config.twin_api_key, config.twin_api_base,
                     timeout=config.twin_timeout)
