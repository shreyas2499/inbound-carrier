"""Twin database schema — REFERENCE ONLY (not imported, not used at runtime).

A single place to see what Twin tables we plan to create and what columns each
holds, so we don't have to dig through the HappyRobot UI to remember the shape.
Twin is HappyRobot's managed PostgreSQL; in Phase 6 we create these tables there
(UI or REST API) and this file just mirrors them.

Twin supported column types: int8, int4, float8, float4, text, boolean,
timestamp, uuid, jsonb.

Design in one line:
    call_records  -> 1 row per CALL,      workflow writes the call, adapter fills the money
    event_log     -> 1 row per API call,  written by the ADAPTER  -> raw audit log
    carriers      -> 1 row per CARRIER,   upserted by the ADAPTER -> history + OTP home
    otp_challenges-> 1 row per CARRIER,   upserted by the ADAPTER -> live identity-verification code

Rules baked into the schema:
    * call_records stores `margin_vs_ceiling`, NEVER `max_buy`. Dashboard-facing.
    * call_records has TWO writers on one row, keyed on the workflow's run_id. The
      workflow cannot supply loadboard_rate, agreed_rate, margin_vs_ceiling or a
      trustworthy round count -- all four derive from MAX_BUY or from counting
      tool calls, and MAX_BUY never reaches the agent by design. Handing those
      back to the agent to write would leak the ceiling. So the adapter writes
      them directly, which is also why every tool body must carry the run_id.
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


# --- call_records : one row per carrier call ---------------------------------
# TWO writers, ONE row, keyed on run_id.
#
#   1. the WORKFLOW's Write-to-Twin node (insert-or-upsert) fires at the end of
#      every call and writes the call-shaped facts: who called, whether they got
#      through the gates, what was discussed, how it ended.
#   2. the ADAPTER later fills the MONEY columns, because it is the only thing
#      that can. Everything below marked "adapter" derives from MAX_BUY or from
#      counting tool calls server-side; MAX_BUY is deliberately stripped from
#      every agent-facing response, so those numbers do not exist anywhere in
#      the workflow's variable space. Returning them to the agent to write would
#      hand it the ceiling (agreed_rate + margin_vs_ceiling == MAX_BUY), which
#      is the one thing the negotiation design must never leak.
#
# Two consequences of writing from the workflow rather than using a Workflow
# Dump table: abandoned calls still produce a row (a dump only fires on a
# successful run), and run_id as the primary key makes the write idempotent.
@dataclass
class CallRecord:
    """Summary of one carrier call. Source for the Northstar KPIs and the ops
    app's per-call trail. Column comments name the exact workflow variable each
    value comes from, so the Write-to-Twin node can be mapped without guesswork."""

    # -- identity / provenance (workflow) ------------------------------------
    run_id: str                        # text      pk   <- Current.run_id
    started_at: datetime               # timestamp      <- Handle Customer Call.now.iso
    environment: Optional[str]         # text           <- Current.execution_environment
                                       #                   development|staging|production.
                                       #                   Twin has no env separation of its
                                       #                   own -- this column IS the separation,
                                       #                   so every KPI filters on it.

    # -- carrier + gates (workflow) ------------------------------------------
    mc_number: Optional[str]           # text           <- Verify Carrier Details.mc_number
                                       #                   the adapter's normalized echo, NOT the
                                       #                   raw verify_carrier.mc_number the agent sent
    carrier_name: Optional[str]        # text           <- Verify Carrier Details.legal_name
    authority_eligible: Optional[bool] # boolean        <- Verify Carrier Details.eligible
    otp_verified: Optional[bool]       # boolean        <- Verify OTP.verified (last call wins,
                                       #                   which is what we want for fail-then-retry)

    # -- what was searched / pitched (workflow) -------------------------------
    load_id: Optional[str]             # text           <- evaluate_offer.load_id
    equipment: Optional[str]           # text           <- search_loads.eqtype
    origin_state: Optional[str]        # text           <- search_loads.orig_state
    dest_state: Optional[str]          # text           <- search_loads.dest_state
                                       #                   STATE only. The city pair lives inside
                                       #                   Search Loads.loads (a jsonb array) and the
                                       #                   Write-to-Twin node cannot index into it.

    # -- last negotiation move (workflow) -------------------------------------
    last_action: Optional[str]         # text           <- Evaluate Offers.action
                                       #                   offer|counter|accept|reject|clarify
    last_rate: Optional[int]           # int4           <- Evaluate Offers.rate
                                       #                   The pair is stored raw because the node
                                       #                   cannot branch; agreed_rate is only
                                       #                   meaningful when last_action == 'accept'.

    # -- outcome (workflow) ----------------------------------------------------
    outcome: str                       # text           <- call_outcome.response.classification
                                       #                   booked|no_deal|unverified|no_match|
                                       #                   tms_fault|abandoned
    outcome_reason: Optional[str]      # text           <- call_outcome.response.reasoning
                                       #                   one line of WHY; 'no_deal' alone is
                                       #                   unreviewable without it

    # -- call quality (workflow, free from the voice agent) --------------------
    handle_time_sec: Optional[int]     # int4           <- Handle Customer Call.duration
    call_end_initiator: Optional[str]  # text           <- Handle Customer Call.call_end_initiator
                                       #                   non-LLM cross-check on the 'abandoned' tag
    num_tool_calls: Optional[int]      # int4           <- Handle Customer Call.num_tool_calls
    assistant_cut_ratio: Optional[float]  # float4      <- Handle Customer Call.assistant_cut_message_ratio
                                       #                   measures the speech-fragmentation problem
                                       #                   behind the duplicate-evaluate_offer bug
    p90_latency_ms: Optional[int]      # int4           <- Handle Customer Call.p90_latency_ms

    # -- LLM cross-check, NOT source of truth (workflow) -----------------------
    extracted_agreed_rate: Optional[int]  # int4        <- negotiation.response.agreed_rate
    extracted_rounds: Optional[int]       # int4        <- negotiation.response.negotiation_rounds
                                       #                   Re-read off the transcript by the Extract
                                       #                   node. Kept to diff against the adapter's
                                       #                   own numbers (test-scenarios 9.12). If the
                                       #                   two ever disagree, the ADAPTER is right.

    # -- MONEY: adapter-written, keyed on run_id -------------------------------
    loadboard_rate: Optional[int]      # int4      posted rate, whole dollars
    agreed_rate: Optional[int]         # int4      final rate, written only on an 'accept'
    margin_vs_ceiling: Optional[int]   # int4      max_buy - agreed_rate. NEVER max_buy itself.
    rounds: Optional[int]              # int4      offers actually made, counted server-side per
                                       #           (run_id, load_id). Trustworthy in a way that
                                       #           evaluate_offer.round is not: that field is the
                                       #           AGENT's own count of itself, and we have a run
                                       #           where it skipped a rung.

    # NOTE -- booking_ref is deliberately absent. POST /tools/book_load exists on
    # the adapter and returns one, but NO workflow tool node calls it: the agent
    # negotiates a rate and hands off to a (mocked) senior rep without ever
    # writing to the TMS. So outcome='booked' currently means "a rate was agreed",
    # not "the load is booked". Add the column together with the book_load tool
    # node, or not at all -- an always-null column is worse than no column.


# --- event_log : one row per API/tool call (writer: adapter) -----------------
@dataclass
class EventLog:
    """One raw API interaction (FMCSA or TMS), written fire-and-forget by the
    adapter. The audit/debug trail — verbatim FULL upstream request + response,
    incl. faults and latency. NOT on the dashboard. INTERNAL ONLY (payloads can
    hold max_buy). Secrets are stripped from `request` before writing."""

    id: str                            # uuid      pk, DB default gen_random_uuid()
    ts: datetime                       # timestamp when the call happened (UTC),
                                       #           DB default now(). The adapter sends neither
                                       #           of the above -- log_event() omits them on
                                       #           purpose, so the defaults are load-bearing.
    environment: Optional[str]         # text      development|staging|production; the adapter
                                       #           only knows it if the workflow passes the
                                       #           Execution Environment global in the tool body
    run_id: Optional[str]              # text      -> call_records.run_id. Same name in all three
                                       #           places (workflow global, this column,
                                       #           obs.call_context) so the join needs no
                                       #           translation layer.
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

    id: str                            # uuid      pk, DB default gen_random_uuid()
    mc_number: str                     # text      natural key, UNIQUE NOT NULL in the DB.
                                       #           The constraint is what makes upsert_carrier
                                       #           safe under concurrent calls -- without it the
                                       #           read-then-write scan can double-insert.
    dot_number: Optional[str]          # text      from FMCSA
    legal_name: Optional[str]          # text      from FMCSA
    authority_eligible: Optional[bool] # boolean   last known FMCSA result
    phone: Optional[str]               # text      FMCSA-registered phone (often null)
    email: Optional[str]               # text      OTP contact-of-record (future; from onboarding)
    first_seen: datetime               # timestamp first interaction (UTC), DB default now()
    last_seen: datetime                # timestamp most recent interaction (UTC), DB default now();
                                       #           on update the adapter sends an ISO-8601 string,
                                       #           never the SQL text "now()" -- these go out as
                                       #           JSON and are stored verbatim, not evaluated.
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

    id: str                            # uuid      pk, DB default gen_random_uuid()
    mc_number: str                     # text      natural key, UNIQUE NOT NULL -> carriers.mc_number
    code_hash: str                     # text      NOT NULL. Named for what it must contain: the
                                       #           column is the constraint. A column called `code`
                                       #           plus a comment saying "hash it first" is an
                                       #           invitation to store a live secret in the clear.
    run_id: Optional[str]              # text      -> call_records.run_id (audit)
    created_at: datetime               # timestamp when issued (UTC), DB default now()
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
    run_id: str                        # text      -> call_records.run_id
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
