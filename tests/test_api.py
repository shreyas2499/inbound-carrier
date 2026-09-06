"""Endpoint tests against a mock TMS. The headline assertions: MAX_BUY never
appears in any agent-facing response, and auth is enforced."""
import pytest

from adapter.config import Config
from adapter.app import create_app
from adapter.tms_client import TmsClient
from mock_tms import MockTms

# LOAD_GET carries the ceiling on the wire (MAX_BUY:0002500) — the adapter must strip it.
GET_WITH_CEILING = (
    b"LOAD_ID:LD0000045821|ORIG_STATE:GA|RATE:0002150|STATUS:OPEN |MAX_BUY:0002500\r\nEND\r\n"
)
QUERY_RESULT = (
    b"LOAD_ID:LD0000045821|ORIG_STATE:GA|DEST_STATE:TX|EQTYPE:DRY_VAN |"
    b"RATE:0002150|MILES:000785|STATUS:OPEN\r\nEND\r\n"
)
BOOK_OK = (
    b"LOAD_ID:LD0000045821|BOOKING_REF:BR00000000091277|STATUS:BOOKED |"
    b"TIMESTAMP:20260504193122\r\nEND\r\n"
)
API_KEY = "secret-key"


def _make(handler, host_port_holder):
    mock = MockTms(handler)
    host, port = mock.__enter__()
    host_port_holder.append(mock)
    cfg = Config(tms_host=host, tms_port=port, tms_token="t-test", fmcsa_api_key="",
                 adapter_api_key=API_KEY, tms_timeout=0.5, tms_max_retries=0,
                 twin_api_key="", twin_api_base="", twin_timeout=0.5)
    client = TmsClient(host, port, "t-test", timeout=0.5, max_retries=0)
    return create_app(client=client, config=cfg).test_client()


@pytest.fixture
def mocks():
    holder = []
    yield holder
    for m in holder:
        m.stop()


def _route_handler(req):
    if "CMD:LOAD_QUERY" in req:
        return (QUERY_RESULT, 0)
    if "CMD:LOAD_GET" in req:
        return (GET_WITH_CEILING, 0)
    if "CMD:LOAD_BOOK" in req:
        return (BOOK_OK, 0)
    return (b"ERR|CODE:UNKNOWN_CMD|MSG:no\r\n", 0)


def test_health_needs_no_auth(mocks):
    c = _make(_route_handler, mocks)
    assert c.get("/health").get_json() == {"status": "ok"}


def test_tools_require_api_key(mocks):
    c = _make(_route_handler, mocks)
    r = c.post("/tools/get_load", json={"load_id": "LD0000045821"})   # no key
    assert r.status_code == 401


def test_get_load_strips_max_buy(mocks):
    c = _make(_route_handler, mocks)
    r = c.post("/tools/get_load", json={"load_id": "LD0000045821"},
               headers={"X-API-Key": API_KEY})
    body = r.get_json()
    assert r.status_code == 200
    assert body["load"]["ORIG_STATE"] == "GA"     # ordinary fields still pass through
    assert "RATE" not in body["load"]             # posted rate withheld so the agent cannot anchor on it
    assert "MAX_BUY" not in body["load"]          # the ceiling never reaches the agent


def test_search_loads_rejects_equipment_only_search(mocks):
    """Equipment with no geography is how a caller who never said where they were
    got pitched a 3,832-mile Alaska run. The server refuses it and says what to
    ask for, so a forgotten question cannot become a confident wrong pitch."""
    c = _make(_route_handler, mocks)
    r = c.post("/tools/search_loads", json={"eqtype": "DRY_VAN"},
               headers={"X-API-Key": API_KEY})
    assert r.status_code == 400
    body = r.get_json()
    assert body["error"] == "missing_field"
    assert "origin" in body["message"].lower()


def test_search_loads_allows_open_destination(mocks):
    """'I'm in Georgia, I'll go anywhere' is a real answer -- origin alone is a
    valid search, only the origin is mandatory."""
    c = _make(_route_handler, mocks)
    r = c.post("/tools/search_loads", json={"eqtype": "DRY_VAN", "orig_state": "GA"},
               headers={"X-API-Key": API_KEY})
    assert r.status_code == 200


def test_search_loads_accepts_origin_city_or_zip(mocks):
    """ORIG_CITY and ORIG_ZIP are geography too -- the guard must not insist on
    the state field specifically."""
    c = _make(_route_handler, mocks)
    for filt in ({"orig_city": "Atlanta"}, {"orig_zip": "30301"}):
        r = c.post("/tools/search_loads", json=filt, headers={"X-API-Key": API_KEY})
        assert r.status_code == 200, filt


def test_search_loads_returns_summaries(mocks):
    c = _make(_route_handler, mocks)
    r = c.post("/tools/search_loads", json={"orig_state": "GA", "dest_state": "TX"},
               headers={"X-API-Key": API_KEY})
    body = r.get_json()
    assert body["count"] == 1
    assert body["loads"][0]["LOAD_ID"] == "LD0000045821"
    assert all("MAX_BUY" not in l for l in body["loads"])


def test_evaluate_offer_uses_ceiling_but_never_returns_it(mocks):
    c = _make(_route_handler, mocks)
    r = c.post("/tools/evaluate_offer",
               json={"load_id": "LD0000045821", "carrier_offer": 2400, "round": 1},
               headers={"X-API-Key": API_KEY})
    body = r.get_json()
    assert r.status_code == 200
    assert body["action"] in ("accept", "counter", "reject")
    if body["rate"] is not None:
        assert body["rate"] <= 2500
    assert "MAX_BUY" not in body and "max_buy" not in body


def test_book_load_success(mocks):
    c = _make(_route_handler, mocks)
    r = c.post("/tools/book_load",
               json={"load_id": "LD0000045821", "mc_number": "872144", "agreed_rate": 2200},
               headers={"X-API-Key": API_KEY})
    body = r.get_json()
    assert body["status"] == "booked"
    assert body["booking_ref"] == "BR00000000091277"


def test_book_ambiguous_is_confirmed_via_get(mocks):
    # BOOK times out; a follow-up GET shows STATUS:BOOKED -> report booked.
    def handler(req):
        if "CMD:LOAD_BOOK" in req:
            return (b"", 1.0)                      # timeout during book
        if "CMD:LOAD_GET" in req:
            return (b"LOAD_ID:LD0000045821|STATUS:BOOKED |BOOKING_REF:BR00000000091277\r\nEND\r\n", 0)
        return (b"ERR|CODE:UNKNOWN_CMD|MSG:no\r\n", 0)

    c = _make(handler, mocks)
    r = c.post("/tools/book_load",
               json={"load_id": "LD0000045821", "mc_number": "872144", "agreed_rate": 2200},
               headers={"X-API-Key": API_KEY})
    body = r.get_json()
    assert body["status"] == "booked"
    assert "confirmed" in body.get("note", "")


def test_evaluate_offer_opening_returns_offer_below_ceiling(mocks):
    c = _make(_route_handler, mocks)
    r = c.post("/tools/evaluate_offer", json={"load_id": "LD0000045821", "round": 0},
               headers={"X-API-Key": API_KEY})
    body = r.get_json()
    assert r.status_code == 200
    assert body["action"] == "offer"
    assert body["rate"] < 2500                    # opens below the (2500) ceiling
    assert "MAX_BUY" not in body and "max_buy" not in body


def test_evaluate_offer_caches_load_within_call(mocks):
    calls = {"get": 0}

    def handler(req):
        if "CMD:LOAD_GET" in req:
            calls["get"] += 1
        return (GET_WITH_CEILING, 0)

    c = _make(handler, mocks)
    for _ in range(2):
        r = c.post("/tools/evaluate_offer",
                   json={"load_id": "LD0000045821", "carrier_offer": 2000, "round": 1},
                   headers={"X-API-Key": API_KEY})
        assert r.status_code == 200
    assert calls["get"] == 1                       # 2nd round served from cache, not the TMS


def test_verify_carrier_endpoint_eligible(mocks, monkeypatch):
    from adapter import fmcsa

    class R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"content": [{"carrier": {"allowedToOperate": "Y", "outOfService": "N",
                                             "legalName": "ACME TRUCKING", "dotNumber": 1,
                                             "telephone": "5551234567"}}]}

    monkeypatch.setattr(fmcsa.requests, "get", lambda *a, **k: R())
    c = _make(_route_handler, mocks)
    r = c.post("/tools/verify_carrier", json={"mc_number": "872144"},
               headers={"X-API-Key": API_KEY})
    body = r.get_json()
    assert r.status_code == 200
    assert body["eligible"] is True
    assert body["legal_name"] == "ACME TRUCKING"
    assert body["phone"] == "5551234567"


def _fmcsa_stub(monkeypatch, *, allowed="Y", oos_date=None):
    """Point fmcsa at a canned SAFER response so the route can be exercised.
    Note the out-of-service signal is `oosDate`, not an `outOfService` flag --
    this endpoint has no such flag, and stubbing the wrong key silently produces
    an ELIGIBLE carrier."""
    from adapter import fmcsa

    class R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"content": [{"carrier": {"allowedToOperate": allowed,
                                             "oosDate": oos_date,
                                             "legalName": "ACME TRUCKING",
                                             "dotNumber": 1,
                                             "telephone": "5551234567"}}]}

    monkeypatch.setattr(fmcsa.requests, "get", lambda *a, **k: R())


def _spy_on_issue(monkeypatch):
    """Replace otp.issue with a recorder. Returns the list it appends MCs to."""
    from adapter import routes

    seen = []

    def fake_issue(client, mc_number, run_id=None):
        seen.append(str(mc_number))
        return {"sent": True, "mc_number": str(mc_number)}

    monkeypatch.setattr(routes.otp, "issue", fake_issue)
    return seen


def test_verify_carrier_issues_code_when_eligible(mocks, monkeypatch):
    """The happy path sends the identity code server-side, so the agent has no
    tool call it can forget to make."""
    _fmcsa_stub(monkeypatch)
    seen = _spy_on_issue(monkeypatch)
    c = _make(_route_handler, mocks)
    r = c.post("/tools/verify_carrier", json={"mc_number": "872144"},
               headers={"X-API-Key": API_KEY})
    body = r.get_json()
    assert body["eligible"] is True
    assert body["otp_sent"] is True
    assert seen == ["872144"]


def test_verify_carrier_issues_no_code_when_not_eligible(mocks, monkeypatch):
    """No authority, no code. Otherwise this endpoint becomes a way to spray
    challenges at any MC number a caller cares to read out."""
    _fmcsa_stub(monkeypatch, allowed="N")
    seen = _spy_on_issue(monkeypatch)
    c = _make(_route_handler, mocks)
    r = c.post("/tools/verify_carrier", json={"mc_number": "872144"},
               headers={"X-API-Key": API_KEY})
    body = r.get_json()
    assert body["eligible"] is False
    assert body["otp_sent"] is False
    assert seen == []


def test_verify_carrier_issues_no_code_when_out_of_service(mocks, monkeypatch):
    _fmcsa_stub(monkeypatch, oos_date="2026-01-15")
    seen = _spy_on_issue(monkeypatch)
    c = _make(_route_handler, mocks)
    r = c.post("/tools/verify_carrier", json={"mc_number": "872144"},
               headers={"X-API-Key": API_KEY})
    assert r.get_json()["otp_sent"] is False
    assert seen == []


def test_verify_carrier_reports_otp_failure(mocks, monkeypatch):
    """A store failure must not read as a delivered code -- the agent has to know
    to fall back to send_otp rather than tell the caller to check their phone."""
    from adapter import routes
    _fmcsa_stub(monkeypatch)
    monkeypatch.setattr(routes.otp, "issue",
                        lambda *a, **k: {"sent": False, "reason": "store_unavailable"})
    c = _make(_route_handler, mocks)
    body = c.post("/tools/verify_carrier", json={"mc_number": "872144"},
                  headers={"X-API-Key": API_KEY}).get_json()
    assert body["eligible"] is True
    assert body["otp_sent"] is False
    assert "code" not in body            # never, under any circumstances


def test_verify_carrier_requires_mc(mocks):
    c = _make(_route_handler, mocks)
    r = c.post("/tools/verify_carrier", json={}, headers={"X-API-Key": API_KEY})
    assert r.status_code == 400
