"""HTTP routes — thin gateways translating HTTP <-> the TMS client and the
negotiation policy. No business logic beyond request/response plumbing: the
client handles TCP + faults, the codec handles the wire, the negotiation module
owns the ceiling math, the serializer owns the public shape, the cache spares the
flaky TMS during a single call.

Two guarantees enforced at this layer: MAX_BUY never leaves the server, and every
/tools/* endpoint requires the adapter API key.
"""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from adapter.auth import require_api_key
from adapter.negotiation import evaluate_offer as evaluate_offer_policy
from adapter.serializers import public_load
from adapter.tms_client import BookAmbiguousError, TmsUnavailable
from adapter.tms_codec import TmsError

bp = Blueprint("tools", __name__)


def _client():
    return current_app.config["TMS_CLIENT"]


def _cache():
    return current_app.config["LOAD_CACHE"]


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


@bp.get("/health")
def health():
    return jsonify(status="ok")


@bp.post("/tools/search_loads")
@require_api_key
def search_loads():
    body = request.get_json(silent=True) or {}
    filters = {k.upper(): v for k, v in body.items() if v not in (None, "")}
    if not filters:
        return jsonify(error="missing_field", message="at least one filter required"), 400
    try:
        loads = _client().load_query(**filters)
    except TmsError as e:
        return jsonify(error=e.code, message=e.message), 502
    except TmsUnavailable as e:
        return jsonify(error="tms_unavailable", message=str(e)), 503
    return jsonify(loads=[public_load(l) for l in loads], count=len(loads))


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
    # The response carries ONLY the next move — never MAX_BUY.
    return jsonify(**decision)


@bp.post("/tools/book_load")
@require_api_key
def book_load():
    body = request.get_json(silent=True) or {}
    try:
        load_id = body["load_id"]
        mc_number = str(body["mc_number"])
        agreed_rate = int(body["agreed_rate"])
    except (KeyError, TypeError, ValueError):
        return jsonify(error="missing_field",
                       message="load_id, mc_number, agreed_rate required"), 400
    try:
        rec = _client().load_book(load_id, mc_number, agreed_rate)
        _cache().invalidate(load_id)  # availability changed
        return jsonify(status="booked", booking_ref=rec.get("BOOKING_REF"))
    except TmsError as e:
        return jsonify(error=e.code, message=e.message), 409
    except BookAmbiguousError:
        # A timed-out book may have committed. Confirm live (bypass cache).
        try:
            load = _client().load_get(load_id)
        except Exception:
            load = None
        _cache().invalidate(load_id)
        if load and load.get("STATUS") == "BOOKED":
            return jsonify(status="booked", booking_ref=load.get("BOOKING_REF"),
                           note="confirmed after ambiguous booking")
        return jsonify(status="uncertain", error="book_ambiguous",
                       message="booking could not be confirmed; needs review"), 503
