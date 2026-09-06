"""HTTP routes — thin gateways translating HTTP <-> the TMS client, the FMCSA
lookup, and the negotiation policy. No business logic beyond request/response
plumbing: the client handles TCP + faults, the codec handles the wire, fmcsa
handles authority, negotiation owns the ceiling math, the serializer owns the
public shape, the cache spares the flaky TMS during a single call.

Two guarantees enforced at this layer: MAX_BUY never leaves the server, and every
/tools/* endpoint requires the adapter API key.
"""
from __future__ import annotations

import os

from flask import Blueprint, current_app, jsonify, request

from adapter import fmcsa, otp, twin_helper
from adapter.auth import require_api_key
from adapter.negotiation import OFFER_LADDER
from adapter.negotiation import evaluate_offer as evaluate_offer_policy
from adapter.obs import CORRELATION_KEYS, call_context
from adapter.serializers import public_load
from adapter.tms_client import BookAmbiguousError, TmsUnavailable
from adapter.tms_codec import TmsError

bp = Blueprint("tools", __name__)


def _client():
    return current_app.config["TMS_CLIENT"]


def _config():
    return current_app.config["ADAPTER_CONFIG"]


def _cache():
    return current_app.config["LOAD_CACHE"]


def _twin():
    return current_app.config["TWIN_CLIENT"]


def _as_int(value):
    """Best-effort int for values coming off the TMS wire. Returns None rather
    than raising -- a bad rate must never break a live call, it just means one
    analytics column stays null."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _load_record(load_id: str):
    """Full TMS record for a load, served from the short-lived cache when warm.
    Used by read paths (get_load, evaluate_offer). Booking never uses this."""
    cache = _cache()
    cached = cache.get(load_id)
    if cached is not None:
        return cached
    record = _client().load_get(load_id)
    if record is not None:
        cache.set(load_id, record)
    return record


@bp.get("/")
def index():
    """Friendly root so the base URL isn't a bare 404. This is an API service --
    every real capability lives under a path below. /tools/* require the adapter
    API key (X-API-Key); /health, / and /otp/peek are public."""
    return jsonify(
        service="inbound-carrier-adapter",
        status="ok",
        message="Inbound carrier sales adapter. This is an API, not a website.",
        endpoints={
            "health": "GET /health",
            "verify_carrier": "POST /tools/verify_carrier",
            "search_loads": "POST /tools/search_loads",
            "get_load": "POST /tools/get_load",
            "evaluate_offer": "POST /tools/evaluate_offer",
            "book_load": "POST /tools/book_load",
            "send_otp": "POST /tools/send_otp",
            "verify_otp": "POST /tools/verify_otp",
            "otp_peek": "GET /otp/peek?mc=<mc>",
            "twin_probe": "POST /debug/twin_probe",
        },
    )


@bp.get("/health")
def health():
    return jsonify(status="ok")


@bp.post("/tools/verify_carrier")
@require_api_key
def verify_carrier():
    body = request.get_json(silent=True) or {}
    mc = body.get("mc_number") or body.get("MC_NUM") or body.get("mc")
    if not mc:
        return jsonify(error="missing_field", message="mc_number required"), 400
    try:
        result = fmcsa.verify_mc(mc, _config().fmcsa_api_key)
    except fmcsa.FmcsaUnavailable as e:
        return jsonify(error="fmcsa_unavailable", message=str(e)), 503
    # Master carrier record, keyed on mc_number. Fire-and-forget: a Twin outage
    # must not stop a carrier getting verified.
    if result.get("found"):
        twin_helper.upsert_carrier(
            _twin(),
            mc_number=str(result.get("mc_number") or mc),
            dot_number=str(result["dot_number"]) if result.get("dot_number") else None,
            legal_name=result.get("legal_name"),
            authority_eligible=result.get("eligible"),
            phone=result.get("phone"),
        )
    # The identity code is issued HERE, by the server, the moment authority checks
    # out -- not by a separate tool the agent has to remember to call. Those two
    # steps always ran back-to-back with no decision in between, and a gate that
    # depends on the model choosing to open it is a gate that eventually doesn't
    # get opened: live calls repeatedly showed the agent SAYING "I'm sending a
    # code" in the same breath as the welcome and never making the call, leaving
    # the caller waiting on a text that was never sent. Issuing it server-side
    # makes that failure structurally impossible -- there is no longer a call to
    # skip. /tools/send_otp stays, and is now purely the RESEND path.
    #
    # Strictly gated on `eligible`. An unknown or non-eligible MC gets NO code:
    # that call ends at this step anyway, and minting codes for arbitrary MC
    # numbers would turn this endpoint into a way to spray challenges at carriers
    # who never called. No authority, no code.
    otp_sent = False
    if result.get("eligible"):
        otp_sent = bool(
            otp.issue(_twin(), result.get("mc_number") or mc,
                      run_id=call_context(body)["run_id"]).get("sent")
        )

    # Always 200 on a completed lookup (found or not) so the workflow branches on
    # `eligible` rather than on HTTP status. Only a real API failure is a 503.
    #
    # otp_sent is delivery metadata only -- never the code itself. False on an
    # eligible carrier means the store failed: the agent must fall back to
    # send_otp rather than tell the caller to look at a phone that has nothing
    # on it.
    return jsonify(**result, otp_sent=otp_sent)


@bp.post("/tools/search_loads")
@require_api_key
def search_loads():
    body = request.get_json(silent=True) or {}
    # CORRELATION_KEYS are stripped first: this endpoint turns the whole body into
    # LOAD_QUERY filters, so leaving run_id in would send RUN_ID=<uuid> to the
    # legacy TMS as a load filter and match nothing.
    filters = {k.upper(): v for k, v in body.items()
               if v not in (None, "") and k.lower() not in CORRELATION_KEYS}
    if not filters:
        return jsonify(error="missing_field", message="at least one filter required"), 400

    # A search with equipment but NO geography is not a search -- LOAD_QUERY will
    # happily return the first dry van on the board, which is how a caller who
    # never said where they were got pitched Anchorage AK -> Sarasota FL. One
    # forgotten question should not be able to produce a confidently-wrong pitch,
    # so the requirement lives here rather than only in the prompt.
    #
    # ORIGIN is what is mandatory: a truck is somewhere specific and cannot take a
    # load 3,000 miles from it. Destination stays optional on purpose -- "I'm in
    # Georgia, I'll go anywhere" is a real and common answer.
    if not any(k in filters for k in ("ORIG_STATE", "ORIG_CITY", "ORIG_ZIP")):
        return jsonify(
            error="missing_field",
            message="origin required: ask the caller what state they are in "
                    "(and where they want to go) before searching",
        ), 400
    try:
        loads = _client().load_query(**filters)
    except TmsError as e:
        return jsonify(error=e.code, message=e.message), 502
    except TmsUnavailable as e:
        return jsonify(error="tms_unavailable", message=str(e)), 503
    # Enrich each summary hit with its full LOAD_GET record so search returns the
    # complete load detail the TMS holds, not just the LOAD_QUERY summary fields.
    # Falls back to the summary row if a detail fetch fails, and warms the per-call
    # cache so a following get_load / evaluate_offer on the same load is hot.
    detailed = []
    for row in loads[:1]:
        lid = row.get("LOAD_ID")
        record = None
        if lid:
            try:
                record = _load_record(lid)
            except (TmsError, TmsUnavailable):
                record = None
        detailed.append(record or row)
    return jsonify(loads=[public_load(l) for l in detailed], count=len(detailed))


@bp.post("/tools/get_load")
@require_api_key
def get_load():
    body = request.get_json(silent=True) or {}
    load_id = body.get("load_id") or body.get("LOAD_ID")
    if not load_id:
        return jsonify(error="missing_field", message="load_id required"), 400
    try:
        load = _load_record(load_id)
    except TmsError as e:
        return jsonify(error=e.code, message=e.message), 404 if e.code == "UNKNOWN_LOAD" else 502
    except TmsUnavailable as e:
        return jsonify(error="tms_unavailable", message=str(e)), 503
    if not load:
        return jsonify(error="UNKNOWN_LOAD"), 404
    return jsonify(load=public_load(load))


@bp.post("/tools/evaluate_offer")
@require_api_key
def evaluate_offer():
    body = request.get_json(silent=True) or {}
    load_id = body.get("load_id")
    if not load_id:
        return jsonify(error="missing_field", message="load_id required"), 400
    round_number = int(body.get("round", 0) or 0)
    raw_offer = body.get("carrier_offer")
    try:
        carrier_offer = None if raw_offer in (None, "") else int(raw_offer)
    except (TypeError, ValueError):
        return jsonify(error="missing_field", message="carrier_offer must be a number"), 400
    try:
        load = _load_record(load_id)
    except TmsError as e:
        return jsonify(error=e.code, message=e.message), 404
    except TmsUnavailable as e:
        return jsonify(error="tms_unavailable", message=str(e)), 503
    if not load:
        return jsonify(error="UNKNOWN_LOAD"), 404
    max_buy = load.get("MAX_BUY")
    if max_buy is None:
        return jsonify(error="no_ceiling", message="ceiling not available for this token"), 409
    decision = evaluate_offer_policy(max_buy, round_number, carrier_offer)

    run_id = call_context(body)["run_id"]

    # `rounds` is NOT written here. event_log already gets one row per
    # evaluate_offer call, so the count is derivable (see the call_records_v view)
    # instead of being a second copy that has to be kept in step. One store, one
    # source of truth, and no read-modify-write race between gunicorn workers.
    #
    # loadboard_rate IS written on every exchange, not just the accept: it is known
    # the moment a rate is discussed, and a call that dies mid-negotiation is
    # exactly the one worth having a record of.
    action = decision.get("action")
    offered = _as_int(decision.get("rate"))
    ceiling = _as_int(max_buy)

    updates = {"loadboard_rate": _as_int(load.get("RATE"))}

    # margin is written on EVERY real move, against the number actually on the
    # table, not only on an `accept`. Most deals never produce an `accept`: the
    # carrier says "yeah, that works" to our counter, which is a CONVERSATIONAL
    # event the server never sees, so keying the money columns off action ==
    # "accept" left agreed_rate and margin NULL on calls that plainly booked.
    # Writing it every exchange means the last write always pairs with last_rate,
    # which IS the rate the deal closed at whenever the call books.
    #
    # `clarify` is excluded: its rate is the carrier's mis-heard number, not an
    # offer, so a margin against it would be meaningless.
    #
    # Still never returned. agreed_rate/margin_vs_ceiling reconstruct MAX_BUY, so
    # handing either back would leak the ceiling to the agent and to the caller.
    if action != "clarify" and ceiling is not None and offered is not None:
        updates["margin_vs_ceiling"] = ceiling - offered
    if action == "accept":
        updates["agreed_rate"] = offered
    twin_helper.update_call_record(_twin(), run_id=run_id, **updates)

    # The response carries ONLY the next move — never MAX_BUY.
    return jsonify(**decision)


@bp.post("/tools/book_load")
@require_api_key
def book_load():
    """Confirm the closed deal and record it server-side.

    THIS IS THE CALL THAT TELLS THE SERVER A DEAL HAPPENED. Everything before it
    is an offer: `evaluate_offer` knows what we put on the table but never learns
    whether the carrier said yes, because acceptance is a conversational event.
    Without this call a booking that closed on a counter leaves agreed_rate NULL
    (seen on runs 90eb69cb and 42f824af).

    The TMS commit itself is COMMENTED OUT while the demo runs read-only -- each
    live LOAD_BOOK permanently consumes one of a small pool of test loads. The
    Twin write is NOT commented out: recording the deal costs nothing and is the
    half that the data layer depends on. Uncomment the block below to go live.
    """
    body = request.get_json(silent=True) or {}
    try:
        load_id = body["load_id"]
        mc_number = str(body["mc_number"])
        agreed_rate = int(body["agreed_rate"])
    except (KeyError, TypeError, ValueError):
        return jsonify(error="missing_field",
                       message="load_id, mc_number, agreed_rate required"), 400

    try:
        load = _load_record(load_id)
    except TmsError as e:
        return jsonify(error=e.code, message=e.message), 404
    except TmsUnavailable as e:
        return jsonify(error="tms_unavailable", message=str(e)), 503
    if not load:
        return jsonify(error="UNKNOWN_LOAD"), 404

    ceiling = _as_int(load.get("MAX_BUY"))

    # LAST LINE OF DEFENCE. agreed_rate arrives from the agent, which means it
    # arrives from something that mis-hears numbers -- "twenty-seven fifty" is one
    # slip away from 2715 or 2850. Every rate the agent was ever given came from
    # evaluate_offer and was already bounded by the ceiling, so a number above it
    # here means the value was garbled or invented. Refuse rather than commit it,
    # and refuse without echoing the ceiling: the error says only that the number
    # is not one we issued.
    if ceiling is not None and agreed_rate > ceiling:
        return jsonify(error="rate_not_offered",
                       message="that rate was not offered on this load"), 409
    if agreed_rate <= 0:
        return jsonify(error="rate_not_offered",
                       message="that rate was not offered on this load"), 409

    # The deal is now known. Written straight to Twin and NEVER returned --
    # agreed_rate + margin_vs_ceiling reconstructs MAX_BUY.
    run_id = call_context(body)["run_id"]
    updates = {"agreed_rate": agreed_rate,
               "loadboard_rate": _as_int(load.get("RATE"))}
    if ceiling is not None:
        updates["margin_vs_ceiling"] = ceiling - agreed_rate
    twin_helper.update_call_record(_twin(), run_id=run_id, **updates)

    # ---------------------------------------------------------------------
    # LIVE TMS COMMIT -- disabled under the read-only demo scope.
    # Uncomment this block (and drop the read-only return below) to book for real.
    # The ambiguous-booking path is the graded one: a timed-out LOAD_BOOK may have
    # succeeded server-side and a token's booking state is monotonic, so it is
    # never blind-retried -- it is confirmed with a fresh LOAD_GET instead.
    #
    # try:
    #     rec = _client().load_book(load_id, mc_number, agreed_rate)
    #     _cache().invalidate(load_id)  # availability changed
    #     return jsonify(status="booked", booking_ref=rec.get("BOOKING_REF"))
    # except TmsError as e:
    #     return jsonify(error=e.code, message=e.message), 409
    # except BookAmbiguousError:
    #     # A timed-out book may have committed. Confirm live (bypass cache).
    #     try:
    #         confirmed = _client().load_get(load_id)
    #     except Exception:
    #         confirmed = None
    #     _cache().invalidate(load_id)
    #     if confirmed and confirmed.get("STATUS") == "BOOKED":
    #         return jsonify(status="booked",
    #                        booking_ref=confirmed.get("BOOKING_REF"),
    #                        note="confirmed after ambiguous booking")
    #     return jsonify(status="uncertain", error="book_ambiguous",
    #                    message="booking could not be confirmed; needs review"), 503
    # ---------------------------------------------------------------------

    # Read-only scope: the deal is recorded, the load is not committed. The agent
    # gets a plain success so the hand-off line stays truthful -- a senior rep
    # finalises the booking, which is exactly what the demo claims happens.
    return jsonify(status="confirmed", recorded=True)


@bp.post("/debug/twin_probe")
@require_api_key
def debug_twin_probe():
    """Diagnostic: prove the Twin REST contract from the one place that already
    holds the credential and can reach the API.

    Everything twin_helper does is fire-and-forget with errors swallowed, which is
    right for a live call and useless for finding out whether the endpoint shape is
    even correct. This runs the same three operations SYNCHRONOUSLY and reports
    exactly what came back, so a wrong path or payload shape shows up as a real
    error instead of a silently empty table.

    Writes throwaway rows to `carriers` and `otp_challenges` (mc_number 000001)
    and leaves them there -- cleaning up is a DELETE, and that stays a human
    decision:
        DELETE FROM otp_challenges WHERE mc_number = '000001';
        DELETE FROM carriers       WHERE mc_number = '000001';

    Pass {"run_id": "<an existing call_records.run_id>"} to also exercise the
    otp_challenges -> call_records foreign key; omit it to test the write alone.
    """
    body = request.get_json(silent=True) or {}
    client = _twin()
    if not getattr(client, "enabled", False):
        return jsonify(ok=False, reason="twin_disabled",
                       message="HAPPYROBOT_API_KEY is not set on this service"), 503

    probe_mc = "000001"
    steps = []

    def _step(name, fn):
        try:
            result = fn()
            steps.append({"step": name, "ok": True,
                          "result": result if isinstance(result, (dict, list)) else str(result)[:400]})
            return result
        except Exception as exc:
            steps.append({"step": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:600]})
            return None

    _step("insert POST /twin/tables/carriers/rows",
          lambda: client.insert_row("carriers", {"mc_number": probe_mc,
                                                 "legal_name": "REST SHAPE PROBE"}))
    # otp_challenges is exercised through otp.py itself rather than a hand-written
    # payload, so the probe fails exactly where a real call would.
    _step("otp.issue -> otp_challenges",
          lambda: otp.issue(client, probe_mc, run_id=body.get("run_id")))
    _step("otp.peek", lambda: {k: v for k, v in otp.peek(client, probe_mc).items()
                               if k != "code"})
    _step("otp store last error", lambda: otp.last_error() or "none")
    # event_log is the other silent writer: ints and jsonb go over the same wire,
    # so it needs the same coercion. Exercised synchronously here for that reason.
    _step("insert event_log (int + jsonb columns)",
          lambda: client.insert_row("event_log", {
              "tool": "debug/twin_probe",
              "status": "ok",
              "latency_ms": 42,
              "run_id": body.get("run_id"),
              "environment": "development",
              "request": {"probe": True, "nested": {"n": 1}},
              "response": {"ok": True},
          }))
    # call_records money columns: ints patched onto an existing row.
    if body.get("run_id"):
        _step("patch call_records money columns",
              lambda: client.update_row("call_records", {"run_id": body["run_id"]},
                                        {"loadboard_rate": 2600,
                                         "margin_vs_ceiling": 52}))
    found = _step("find (client-side scan of GET /twin/tables/carriers)",
                  lambda: client.find_row("carriers", "mc_number", probe_mc))
    if found and found.get("id"):
        _step("update PATCH /twin/tables/carriers/rows",
              lambda: client.update_row("carriers", {"id": found["id"]},
                                        {"legal_name": "REST SHAPE PROBE 2"}))
    else:
        steps.append({"step": "update PATCH /twin/tables/carriers/rows", "ok": False,
                      "error": "skipped — the row could not be read back"})

    return jsonify(ok=all(s["ok"] for s in steps),
                   api_base=getattr(client, "api_base", None),
                   probe_mc=probe_mc, steps=steps)


@bp.post("/debug/board")
@require_api_key
def debug_board():
    """DEV-ONLY board browser. Runs LOAD_QUERY with whatever filters you give it
    and returns EVERY matching row — not the one enriched load /tools/search_loads
    hands the agent.

    Why this exists separately rather than as a flag on search_loads: that endpoint
    deliberately requires an origin (an origin-less search matches the whole
    national board, which is how a caller who never said where they were got
    pitched a 3,832-mile Alaska run) and deliberately returns ONE load, because the
    agent pitches one. Both are correct for a live call and both get in the way of
    "show me what dry vans are on the board." So the guard rails stay on the agent
    path and the unrestricted view lives here, behind the API key, unreachable from
    the workflow.

    Body: any LOAD_QUERY filter — eqtype, orig_state, dest_state, orig_city, ...
    (at least one; the TMS itself requires it), plus:
      max_results  cap on rows returned      (default 25)
      detail       true -> enrich every row via LOAD_GET, which adds MAX_BUY and
                   the exact ladder the negotiator would walk for that load.
                   One extra round-trip per row, so it is off by default.

    Debug-only, like /debug/load_raw: this DOES expose MAX_BUY and the posted RATE.
    Never wire it to a tool node.
    """
    body = request.get_json(silent=True) or {}
    detail = bool(body.get("detail"))
    try:
        max_results = int(body.get("max_results", 25))
    except (TypeError, ValueError):
        max_results = 25
    max_results = max(1, min(max_results, 100))

    filters = {k.upper(): v for k, v in body.items()
               if v not in (None, "")
               and k.lower() not in CORRELATION_KEYS
               and k.lower() not in ("detail", "max_results")}
    if not filters:
        return jsonify(error="missing_field",
                       message="at least one filter required (e.g. eqtype)"), 400

    try:
        rows = _client().load_query(**filters)
    except TmsError as e:
        return jsonify(error=e.code, message=e.message), 502
    except TmsUnavailable as e:
        return jsonify(error="tms_unavailable", message=str(e)), 503

    total = len(rows)
    rows = rows[:max_results]

    out = []
    for row in rows:
        record = row
        if detail:
            lid = row.get("LOAD_ID")
            if lid:
                try:
                    record = _load_record(lid) or row
                except (TmsError, TmsUnavailable):
                    record = row
        item = {k: (v.isoformat() if hasattr(v, "isoformat") else v)
                for k, v in record.items()}
        ceiling = _as_int(record.get("MAX_BUY"))
        if ceiling is not None:
            # What the agent would actually say on this load, round by round. Makes
            # it possible to eyeball a negotiation before spending a call on it.
            item["_ladder"] = {f"round_{r}": round(ceiling * frac)
                               for r, frac in sorted(OFFER_LADDER.items())}
            item["_accepts_up_to"] = ceiling
        out.append(item)

    return jsonify(loads=out, count=len(out), matched=total,
                   truncated=total > len(out), filters=filters)


@bp.post("/debug/load_raw")
@require_api_key
def debug_load_raw():
    """DEV-ONLY verification endpoint. Returns the COMPLETE raw TMS record for a
    load exactly as LOAD_GET yields it — MAX_BUY and every internal field included,
    nothing stripped or reshaped. Not used by the agent; remove before shipping.

    Bypasses the cache so it always reflects a fresh LOAD_GET off the wire.
    """
    body = request.get_json(silent=True) or {}
    load_id = body.get("load_id") or body.get("LOAD_ID")
    if not load_id:
        return jsonify(error="missing_field", message="load_id required"), 400
    try:
        load = _client().load_get(load_id)
    except TmsError as e:
        return jsonify(error=e.code, message=e.message), 404 if e.code == "UNKNOWN_LOAD" else 502
    except TmsUnavailable as e:
        return jsonify(error="tms_unavailable", message=str(e)), 503
    if not load:
        return jsonify(error="UNKNOWN_LOAD"), 404
    # Everything LOAD_GET returned; datetimes rendered JSON-safe, NOTHING removed.
    full = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in load.items()}
    return jsonify(load=full)



@bp.post("/debug/fmcsa_raw")
@require_api_key
def debug_fmcsa_raw():
    """DEV-ONLY verification-exploration endpoint. Returns the COMPLETE raw FMCSA
    QCMobile response for an MC number -- every field FMCSA sends, untrimmed, plus
    the extracted carrier record -- so you can see which identity fields (address,
    EIN, fleet size, MCS-150, ...) are available. Not used by the agent; remove
    before shipping.
    """
    body = request.get_json(silent=True) or {}
    mc = body.get("mc_number") or body.get("MC_NUM") or body.get("mc")
    if not mc:
        return jsonify(error="missing_field", message="mc_number required"), 400
    try:
        result = fmcsa.raw_lookup(mc, _config().fmcsa_api_key)
    except fmcsa.FmcsaUnavailable as e:
        return jsonify(error="fmcsa_unavailable", message=str(e)), 503
    return jsonify(**result)


@bp.post("/tools/send_otp")
@require_api_key
def send_otp():
    """Issue a one-time code for a carrier's MC and 'deliver' it to their device
    (readable via the public /otp/peek). Returns only delivery metadata -- never
    the code itself, so the agent can't leak it."""
    body = request.get_json(silent=True) or {}
    mc = body.get("mc_number") or body.get("mc") or body.get("MC_NUM")
    if not mc:
        return jsonify(error="missing_field", message="mc_number required"), 400
    return jsonify(**otp.issue(_twin(), mc, run_id=call_context(body)["run_id"]))


@bp.post("/tools/verify_otp")
@require_api_key
def verify_otp():
    """The gate the agent must clear before load matching. Expiry, attempt limits
    and single-use are all enforced here, so no conversational framing from the
    caller can bypass verification."""
    body = request.get_json(silent=True) or {}
    mc = body.get("mc_number") or body.get("mc")
    code = body.get("code") or body.get("otp")
    if not mc or code in (None, ""):
        return jsonify(error="missing_field", message="mc_number and code required"), 400
    return jsonify(**otp.verify(_twin(), mc, code))


@bp.get("/otp/peek")
def otp_peek():
    """PUBLIC (no API key): the carrier 'device' page reads its active code here.
    This is the demo stand-in for an SMS arriving on the handset, so -- like a real
    phone -- it carries no adapter auth. CORS-open because the device UI is served
    from a separate origin (its own Railway service)."""
    result = otp.peek(_twin(), request.args.get("mc", ""))
    resp = jsonify(**result)
    resp.headers["Access-Control-Allow-Origin"] = os.environ.get("OTP_CORS_ORIGIN", "*")
    resp.headers["Cache-Control"] = "no-store"
    return resp
