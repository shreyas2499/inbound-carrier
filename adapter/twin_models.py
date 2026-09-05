"""Twin database schema — REFERENCE ONLY (not imported, not used at runtime).

A single place to see what Twin tables we plan to create and what columns each
holds, so we don't have to dig through the HappyRobot UI to remember the shape.
Twin is HappyRobot's managed PostgreSQL; in Phase 6 we create these tables there
(UI or REST API) and this file just mirrors them.

Twin supported column types: int8, int4, float8, float4, text, boolean,
timestamp, uuid, jsonb.

Design in one line:
    call_records  -> 1 row per CALL,      written by the WORKFLOW -> KPI dashboard
    event_log     -> 1 row per API call,  written by the ADAPTER  -> raw audit log
    carriers      -> 1 row per CARRIER,   upserted by the ADAPTER -> history + OTP home
    otp_challenges-> 1 row per CARRIER,   upserted by the ADAPTER -> live identity-verification code

Rules baked into the schema:
    * call_records stores `margin_vs_ceiling`, NEVER `max_buy`. Dashboard-facing.
    * event_log stores the FULL raw upstream payloads (request + response as
      jsonb) — which DO contain max_buy (verbatim get_load). INTERNAL audit only,
      never surfaced to the agent or a carrier-facing app. Secrets (TMS AUTH
      token, FMCSA webKey) are NEVER written to the request payload.
    * Twin has NO dev/staging/prod separation of its own -- it is one database per
      workspace (unlike Workflows/Runs, which are environment-scoped). Rows carry
      an `environment` column instead, populated from the workflow's built-in
      `Execution Environment` global, so KPIs can filter to production and test
      traffic stays auditable rather than discarded.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional


# --- call_records : one row per carrier call (writer: workflow) --------------
@dataclass
class CallRecord:
    """Summary of one carrier call, written once at the end of the call by the
    workflow's Write-to-Twin node. Source for the Northstar KPIs and the ops
    app's per-call trail."""

    id: str                            # uuid      pk
    started_at: datetime               # timestamp call start (UTC)
    environment: Optional[str]         # text      development|staging|production
                                       #           (workflow global: Execution Environment)
    mc_number: Optional[str]           # text      carrier identity (null if never given)
    carrier_name: Optional[str]        # text      legal name from FMCSA
    authority_eligible: Optional[bool] # boolean   FMCSA authority result
    otp_verified: Optional[bool]       # boolean   OTP outcome (once OTP exists)
    load_id: Optional[str]             # text      load pitched (null if none matched)
    equipment: Optional[str]           # text      e.g. DRY_VAN, REEFER
    origin: Optional[str]              # text      "City, ST"
    destination: Optional[str]         # text      "City, ST"
    loadboard_rate: Optional[int]      # int4      posted rate, whole dollars
    agreed_rate: Optional[int]         # int4      final booked rate (null if no deal)
    margin_vs_ceiling: Optional[int]   # int4      max_buy - agreed_rate  (NOT max_buy itself)
    rounds: Optional[int]              # int4      negotiation rounds used (0-3)
    outcome: str                       # text      booked|no_deal|unverified|no_match|tms_fault|abandoned
    booking_ref: Optional[str]         # text      TMS booking ref on success
    handle_time_sec: Optional[int]     # int4      call duration in seconds


# --- event_log : one row per API/tool call (writer: adapter) -----------------
@dataclass
class EventLog:
    """One raw API interaction (FMCSA or TMS), written fire-and-forget by the
    adapter. The audit/debug trail — verbatim FULL upstream request + response,
    incl. faults and latency. NOT on the dashboard. INTERNAL ONLY (payloads can
    hold max_buy). Secrets are stripped from `request` before writing."""

    id: str                            # uuid      pk
    ts: datetime                       # timestamp when the call happened (UTC)
    environment: Optional[str]         # text      development|staging|production; the adapter
                                       #           only knows it if the workflow passes the
                                       #           Execution Environment global in the tool body
    call_id: Optional[str]             # text      correlates rows to one CallRecord.id
    tool: str                          # text      fmcsa|search_loads|get_load|evaluate_offer|book_load
    mc_number: Optional[str]           # text      denormalized for filtering
    load_id: Optional[str]             # text      denormalized for filtering
    request: dict[str, Any]            # jsonb     logical request (NO auth token / webKey)
    response: dict[str, Any]           # jsonb     ENTIRE raw upstream response, verbatim
    status: str                        # text      ok|not_found|already_booked|invalid_rate|tms_fault|error
    latency_ms: Optional[int]          # int4      round-trip time; feeds a fault/latency view


# --- carriers : one row per carrier, upserted on mc_number (writer: adapter) --
@dataclass
class Carrier:
    """Master record for a carrier, upserted (keyed on mc_number) whenever we run
    an FMCSA lookup. Gives carrier history / dedup across calls, and is the
    natural home for the OTP contact-of-record once OTP is built (in a real
    brokerage the verified contact lives in the broker's own carrier records —
    this table is that)."""

    id: str                            # uuid      pk
    mc_number: str                     # text      natural key (unique)
    dot_number: Optional[str]          # text      from FMCSA
    legal_name: Optional[str]          # text      from FMCSA
    authority_eligible: Optional[bool] # boolean   last known FMCSA result
    phone: Optional[str]               # text      FMCSA-registered phone (often null)
    email: Optional[str]               # text      OTP contact-of-record (future; from onboarding)
    first_seen: datetime               # timestamp first interaction (UTC)
    last_seen: datetime                # timestamp most recent interaction (UTC)
    call_count: int                    # int4      how many times this carrier has called
    fmcsa_raw: Optional[dict[str, Any]]# jsonb     last full FMCSA payload (optional; else see event_log)


# --- otp_challenges : one row per carrier, upserted on mc_number (writer: adapter)
@dataclass
class OtpChallenge:
    """One identity-verification challenge for a carrier, keyed on mc_number and
    upserted (a new send_otp replaces the carrier's prior code) -- the Twin
    equivalent of the adapter's live SQLite store in otp.py. Move the OTP state
    here when it must survive adapter restarts or be shared across replicas
    (the /tmp SQLite store is per-container and ephemeral). Links to
    [carriers].mc_number for the contact-of-record.

    SECURITY: the in-memory demo store keeps the code in the clear, which is fine
    for a short-lived process-local secret. In a shared DB the code should be
    HASHED at rest -- hash `code` (e.g. sha256 + per-row salt) before writing,
    and compare hashes on verify. Never store a live plaintext OTP in Twin."""

    id: str                            # uuid      pk
    mc_number: str                     # text      natural key (unique) -> carriers.mc_number
    code: str                          # text      the one-time code (HASH before storing in Twin)
    call_id: Optional[str]             # text      correlates to call_records.id (audit)
    created_at: datetime               # timestamp when issued (UTC)
    expires_at: datetime               # timestamp issued + TTL (UTC)
    attempts: int                      # int4      wrong-code guesses so far (locks at OTP_MAX_ATTEMPTS)
    verified: bool                     # boolean   whether a correct code cleared it
    verified_at: Optional[datetime]    # timestamp when verified (null until verified)


# --- negotiation_rounds : OPTIONAL, one row per offer (writer: workflow) -----
# Only add this if we want per-round negotiation analytics. call_records.rounds
# already covers the required KPIs, so treat this as a nice-to-have, not a must.
@dataclass
class NegotiationRound:
    """OPTIONAL. One row per negotiation round within a call."""

    id: str                            # uuid      pk
    call_id: str                       # text      -> call_records.id
    round_number: int                  # int4      1..3
    carrier_offer: Optional[int]       # int4      what the carrier asked this round
    our_offer: Optional[int]           # int4      what the agent said (never above the ceiling)
    action: str                        # text      offer|accept|counter|reject


# --- the tables at a glance ---------------------------------------------------
# table name -> (who writes it, grain)
TABLES = {
    "call_records": ("workflow", "one row per carrier call — drives the KPI dashboard"),
    "event_log": ("adapter", "one row per API/tool call — raw internal audit log"),
    "carriers": ("adapter", "one row per carrier (upsert on mc_number) — history + OTP contact home"),
    "otp_challenges": ("adapter", "one row per carrier (upsert on mc_number) — active identity-verification code"),
    # "negotiation_rounds": ("workflow", "one row per negotiation round"),  # optional
}
