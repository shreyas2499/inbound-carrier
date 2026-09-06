"""The adapter's half of the Twin write.

Three things are load-bearing and all three are silent when they break, because
every Twin call is fire-and-forget with its errors swallowed by design:

  1. run_id arrives in the tool body as a correlation field, NOT a tool argument.
     search_loads turns its whole body into LOAD_QUERY filters, so a leak here
     sends RUN_ID=<uuid> to the legacy TMS and matches nothing.
  2. the money columns are written to Twin and never returned. agreed_rate plus
     margin_vs_ceiling reconstructs MAX_BUY, so leaking them leaks the ceiling.
  3. `rounds` is counted by the adapter. The agent's own `round` field is not
     trustworthy -- there is a logged run where it skipped a rung.
"""
from __future__ import annotations

import json

import pytest

from adapter.app import create_app
from adapter.config import Config

RUN = "abbe42d8-9c0d-46e0-857c-5f66066a629f"
LOAD = {"LOAD_ID": "LD00731", "RATE": 2600, "MAX_BUY": 2802, "EQUIP": "DRY_VAN"}
HEADERS = {"X-API-Key": "KEY", "Content-Type": "application/json"}


class FakeTwin:
    """An in-memory stand-in for Twin. It records every write AND serves reads
    back, because otp.py is now a real client of this store -- verification reads
    what issuance wrote."""

    enabled = True

    def __init__(self):
        self.writes = []
        self._tables = {}
        self._next_id = 1

    def insert_row(self, table, values):
        self.writes.append(("insert", table, values))
        row = {"id": f"row-{self._next_id}", **values}
        self._next_id += 1
        self._tables.setdefault(table, []).append(row)
        return row

    def update_row(self, table, primary_key, updates):
        self.writes.append(("update", table, primary_key, updates))
        for row in self._tables.get(table, []):
            if all(row.get(k) == v for k, v in primary_key.items()):
                row.update(updates)
                return row
        return None

    def find_row(self, table, column, value, **kw):
        for row in self._tables.get(table, []):
            if str(row.get(column)) == str(value):
                return row
        return None

    def rows(self, op, table):
        return [w for w in self.writes if w[0] == op and w[1] == table]

    def stored(self, table):
        return self._tables.get(table, [])


class FakeTms:
    def __init__(self):
        self.last_filters = None

    def load_query(self, **filters):
        self.last_filters = dict(filters)
        return [LOAD]

    def load_get(self, load_id):
        return dict(LOAD, LOAD_ID=load_id)


@pytest.fixture
def client(monkeypatch):
    from adapter import otp
    otp._peek_cache._store.clear()   # process-global; must not leak between tests

    cfg = Config(tms_host="h", tms_port=1, tms_token="t", fmcsa_api_key="k",
                 adapter_api_key="KEY", tms_timeout=1, tms_max_retries=0,
                 twin_api_key="fake", twin_api_base="https://x", twin_timeout=1)
    tms = FakeTms()
    app = create_app(client=tms, config=cfg)
    twin = FakeTwin()
    app.config["TWIN_CLIENT"] = twin
    c = app.test_client()
    c.twin, c.tms = twin, tms
    return c


def _negotiate(client, offers, load_id="LD00731", run_id=RUN):
    """Drive a full ladder. offers[0] is None (the opening ask)."""
    out = []
    for rnd, offer in enumerate(offers):
        body = {"load_id": load_id, "round": rnd, "run_id": run_id}
        if offer is not None:
            body["carrier_offer"] = offer
        out.append(client.post("/tools/evaluate_offer", headers=HEADERS,
                               json=body).get_json())
    return out


# --- 1. correlation fields must not become TMS filters ----------------------

def test_run_id_is_not_forwarded_to_the_tms_as_a_load_filter(client):
    r = client.post("/tools/search_loads", headers=HEADERS, json={
        "eqtype": "DRY_VAN", "orig_state": "UT", "dest_state": "IL",
        "run_id": RUN, "environment": "development",
    })
    assert r.status_code == 200
    assert client.tms.last_filters == {
        "EQTYPE": "DRY_VAN", "ORIG_STATE": "UT", "DEST_STATE": "IL"}


def test_search_still_hides_rate_and_ceiling(client):
    r = client.post("/tools/search_loads", headers=HEADERS,
                    json={"eqtype": "DRY_VAN", "run_id": RUN})
    body = json.dumps(r.get_json())
    assert "MAX_BUY" not in body and '"RATE"' not in body


# --- 2. the money write ------------------------------------------------------

def test_accept_writes_the_money_columns_to_twin(client):
    _negotiate(client, [None, 2900, 2850, 2800, 2750])
    updates = client.twin.rows("update", "call_records")
    assert len(updates) == 5, "one write per exchange"
    _, _, pk, vals = updates[-1]
    assert pk == {"run_id": RUN}
    assert vals == {"loadboard_rate": 2600, "agreed_rate": 2750,
                    "margin_vs_ceiling": 52}


def test_loadboard_rate_and_rounds_land_before_any_deal(client):
    """The first exchange already fills the two columns that are knowable then,
    so a call that dies mid-negotiation still leaves evidence it happened."""
    _negotiate(client, [None])
    _, _, _, vals = client.twin.rows("update", "call_records")[0]
    assert vals == {"loadboard_rate": 2600}
    assert "agreed_rate" not in vals and "margin_vs_ceiling" not in vals


def test_ceiling_never_reaches_the_agent(client):
    responses = _negotiate(client, [None, 2900, 2850, 2800, 2750])
    blob = json.dumps(responses).lower()
    assert "max_buy" not in blob
    assert "margin" not in blob
    assert "2802" not in blob  # the ceiling itself, in any field


def test_no_deal_records_the_negotiation_but_no_agreed_rate(client):
    """A no-deal is a real outcome, not a missing one. rounds and loadboard_rate
    are written; agreed_rate and margin stay absent because there was no deal."""
    _negotiate(client, [None, 2900, 2850, 2800, 3500])  # holds above the ceiling
    updates = client.twin.rows("update", "call_records")
    assert updates, "a lost negotiation must still leave a trail"
    _, _, _, vals = updates[-1]
    assert vals["loadboard_rate"] == 2600
    assert "agreed_rate" not in vals
    assert "margin_vs_ceiling" not in vals


def test_no_write_without_a_run_id(client):
    """No correlation id means no row to patch -- must be a silent no-op, not a
    crash and not a write keyed on None."""
    client.post("/tools/evaluate_offer", headers=HEADERS,
                json={"load_id": "LD00731", "round": 4, "carrier_offer": 2750})
    assert client.twin.rows("update", "call_records") == []


# --- 3. rounds is DERIVED from event_log, not stored --------------------------

def test_every_exchange_leaves_an_event_log_row_to_count(client):
    """`rounds` is no longer a column the adapter maintains. The call_records_v
    view counts event_log rows instead, so what has to hold is that every
    exchange leaves exactly one countable row."""
    _negotiate(client, [None, 2900, 2850, 2800, 2750])
    evaluates = [r for r in client.twin.rows("insert", "event_log")
                 if r[2]["tool"] == "tools/evaluate_offer"]
    assert len(evaluates) == 5
    assert {e[2]["run_id"] for e in evaluates} == {RUN}





# --- 4. event_log mirroring ---------------------------------------------------

def test_every_tool_call_lands_in_event_log(client):
    client.post("/tools/search_loads", headers=HEADERS,
                json={"eqtype": "DRY_VAN", "run_id": RUN, "environment": "production"})
    _negotiate(client, [None, 2750])
    rows = client.twin.rows("insert", "event_log")
    assert len(rows) == 3
    first = rows[0][2]
    assert first["tool"] == "tools/search_loads"
    assert first["run_id"] == RUN
    assert first["environment"] == "production"
    assert first["status"] == "ok"
    assert isinstance(first["latency_ms"], int)


def test_event_log_records_failures_too(client):
    client.post("/tools/evaluate_offer", headers=HEADERS, json={"run_id": RUN})
    rows = client.twin.rows("insert", "event_log")
    assert rows and rows[-1][2]["status"] == "missing_field"


def test_event_log_never_stores_the_ceiling(client):
    _negotiate(client, [None, 2750])
    blob = json.dumps(client.twin.rows("insert", "event_log"))
    assert "MAX_BUY" not in blob and "2802" not in blob


def test_twin_failure_cannot_break_a_tool_call(client):
    """Fire-and-forget means the caller must never see a Twin problem."""
    class Exploding(FakeTwin):
        def insert_row(self, *a, **k): raise RuntimeError("twin down")
        def update_row(self, *a, **k): raise RuntimeError("twin down")

    client.application.config["TWIN_CLIENT"] = Exploding()
    r = client.post("/tools/evaluate_offer", headers=HEADERS,
                    json={"load_id": "LD00731", "round": 4,
                          "carrier_offer": 2750, "run_id": RUN})
    assert r.status_code == 200
    assert r.get_json()["action"] == "accept"


# --- 5. otp_challenges is now the ONLY store ---------------------------------

def test_send_otp_writes_the_challenge_and_returns_no_code(client):
    r = client.post("/tools/send_otp", headers=HEADERS,
                    json={"mc_number": "872144", "run_id": RUN,
                          "environment": "development"})
    assert r.get_json()["sent"] is True
    assert "code" not in r.get_json(), "the agent must never receive the code"

    stored = client.twin.stored("otp_challenges")
    assert len(stored) == 1
    row = stored[0]
    assert row["mc_number"] == "872144" and row["run_id"] == RUN
    assert row["attempts"] == 0 and row["verified"] is False
    assert len(row["code"]) == 6 and row["code"].isdigit()


def test_peek_serves_the_code_back_to_the_device(client):
    client.post("/tools/send_otp", headers=HEADERS,
                json={"mc_number": "872144", "run_id": RUN})
    stored_code = client.twin.stored("otp_challenges")[0]["code"]
    seen = client.get("/otp/peek?mc=872144").get_json()
    assert seen["status"] == "active" and seen["code"] == stored_code


def test_correct_code_verifies_and_is_recorded(client):
    client.post("/tools/send_otp", headers=HEADERS,
                json={"mc_number": "872144", "run_id": RUN})
    code = client.twin.stored("otp_challenges")[0]["code"]
    r = client.post("/tools/verify_otp", headers=HEADERS,
                    json={"mc_number": "872144", "code": code, "run_id": RUN})
    assert r.get_json() == {"verified": True, "reason": "ok"}
    row = client.twin.stored("otp_challenges")[0]
    assert row["verified"] is True and row["verified_at"]


def test_wrong_code_counts_down_then_locks(client):
    client.post("/tools/send_otp", headers=HEADERS,
                json={"mc_number": "872144", "run_id": RUN})
    for expected in (3, 2, 1, 0):
        r = client.post("/tools/verify_otp", headers=HEADERS,
                        json={"mc_number": "872144", "code": "000000"}).get_json()
        assert r["verified"] is False and r["attempts_remaining"] == expected
    locked = client.post("/tools/verify_otp", headers=HEADERS,
                         json={"mc_number": "872144", "code": "000000"}).get_json()
    assert locked["reason"] == "too_many_attempts"


def test_verify_without_an_issued_code_fails(client):
    r = client.post("/tools/verify_otp", headers=HEADERS,
                    json={"mc_number": "999999", "code": "123456"}).get_json()
    assert r == {"verified": False, "reason": "no_code_issued"}


def test_a_resend_replaces_the_previous_challenge(client):
    client.post("/tools/send_otp", headers=HEADERS, json={"mc_number": "872144"})
    first = client.twin.stored("otp_challenges")[0]["code"]
    client.post("/tools/verify_otp", headers=HEADERS,
                json={"mc_number": "872144", "code": "000000"})   # burn an attempt
    client.post("/tools/send_otp", headers=HEADERS, json={"mc_number": "872144"})

    rows = client.twin.stored("otp_challenges")
    assert len(rows) == 1, "one live challenge per carrier, not a new row each time"
    assert rows[0]["attempts"] == 0, "a re-send resets the attempt count"
    assert rows[0]["verified"] is False
    old = client.post("/tools/verify_otp", headers=HEADERS,
                      json={"mc_number": "872144", "code": first}).get_json()
    assert old["verified"] is False, "the superseded code must not still work"


def test_the_gate_fails_closed_when_the_store_is_unreachable(client):
    """With Twin as the only store, an outage must deny verification, never
    grant it. This is the cost of the single-store design, pinned down."""
    class Broken(FakeTwin):
        def find_row(self, *a, **k): raise RuntimeError("twin down")

    client.application.config["TWIN_CLIENT"] = Broken()
    from adapter import otp
    otp._peek_cache._store.clear()
    r = client.post("/tools/verify_otp", headers=HEADERS,
                    json={"mc_number": "872144", "code": "123456"}).get_json()
    assert r == {"verified": False, "reason": "store_unavailable"}


def test_peek_is_cached_so_the_device_poll_does_not_scan_every_time(client):
    """The device page polls ~every 1.5s and Twin has no server-side filter, so
    an uncached peek would be a table scan per poll."""
    client.post("/tools/send_otp", headers=HEADERS, json={"mc_number": "872144"})
    calls = {"n": 0}
    real = client.twin.find_row

    def counting(table, column, value, **kw):
        if table == "otp_challenges":
            calls["n"] += 1
        return real(table, column, value, **kw)

    client.twin.find_row = counting
    for _ in range(5):
        client.get("/otp/peek?mc=872144")
    assert calls["n"] == 1, f"expected one read for five polls, got {calls['n']}"
