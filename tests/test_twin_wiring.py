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
    """Records what would have been written instead of writing it."""

    enabled = True

    def __init__(self):
        self.writes = []

    def insert_row(self, table, values):
        self.writes.append(("insert", table, values))

    def update_row(self, table, primary_key, updates):
        self.writes.append(("update", table, primary_key, updates))

    def find_row(self, table, column, value, **kw):
        return None

    def rows(self, op, table):
        return [w for w in self.writes if w[0] == op and w[1] == table]


class FakeTms:
    def __init__(self):
        self.last_filters = None

    def load_query(self, **filters):
        self.last_filters = dict(filters)
        return [LOAD]

    def load_get(self, load_id):
        return dict(LOAD, LOAD_ID=load_id)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CALL_STATE_DB_PATH", str(tmp_path / "call_state.db"))
    import importlib

    from adapter import call_state
    importlib.reload(call_state)  # pick up the temp DB path

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
                    "margin_vs_ceiling": 52, "rounds": 5}


def test_loadboard_rate_and_rounds_land_before_any_deal(client):
    """The first exchange already fills the two columns that are knowable then,
    so a call that dies mid-negotiation still leaves evidence it happened."""
    _negotiate(client, [None])
    _, _, _, vals = client.twin.rows("update", "call_records")[0]
    assert vals == {"loadboard_rate": 2600, "rounds": 1}
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
    assert vals["rounds"] == 5 and vals["loadboard_rate"] == 2600
    assert "agreed_rate" not in vals
    assert "margin_vs_ceiling" not in vals


def test_no_write_without_a_run_id(client):
    """No correlation id means no row to patch -- must be a silent no-op, not a
    crash and not a write keyed on None."""
    client.post("/tools/evaluate_offer", headers=HEADERS,
                json={"load_id": "LD00731", "round": 4, "carrier_offer": 2750})
    assert client.twin.rows("update", "call_records") == []


# --- 3. server-side round counting -------------------------------------------

def test_rounds_counts_exchanges_not_the_agents_claim(client):
    """Replays the real duplicate-call bug: the agent fired evaluate_offer twice
    for round 3 on the same number (speech fragmentation), then closed at round 4.
    Its own bookkeeping would say 5 exchanges; there were actually 6. The adapter
    counts calls, so it is right and the agent's `round` field is irrelevant."""
    calls = [(0, None), (1, 2900), (2, 2850), (3, 2800), (3, 2800), (4, 2750)]
    for rnd, offer in calls:
        body = {"load_id": "LD00731", "round": rnd, "run_id": RUN}
        if offer is not None:
            body["carrier_offer"] = offer
        client.post("/tools/evaluate_offer", headers=HEADERS, json=body)
    _, _, _, vals = client.twin.rows("update", "call_records")[-1]
    assert vals["rounds"] == 6
    assert vals["agreed_rate"] == 2750


def test_round_counters_are_per_load(client):
    from adapter import call_state
    _negotiate(client, [None, 2900], load_id="LD00731")
    _negotiate(client, [None], load_id="LD00999")
    assert call_state.round_count(RUN, "LD00731") == 2
    assert call_state.round_count(RUN, "LD00999") == 1


def test_round_counters_are_per_call(client):
    from adapter import call_state
    _negotiate(client, [None, 2900], run_id="run-A")
    _negotiate(client, [None], run_id="run-B")
    assert call_state.round_count("run-A", "LD00731") == 2
    assert call_state.round_count("run-B", "LD00731") == 1


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


# --- 5. otp_challenges mirror -------------------------------------------------

def test_send_otp_mirrors_the_challenge_without_the_code(client):
    r = client.post("/tools/send_otp", headers=HEADERS,
                    json={"mc_number": "872144", "run_id": RUN,
                          "environment": "development"})
    assert r.get_json()["sent"] is True
    assert "code" not in r.get_json(), "the agent must never receive the code"

    rows = client.twin.rows("insert", "otp_challenges")
    assert len(rows) == 1
    row = rows[0][2]
    assert row["mc_number"] == "872144"
    assert row["run_id"] == RUN
    assert row["verified"] is False and row["attempts"] == 0
    assert len(row["code_hash"]) == 64          # sha-256 hex
    assert "code" not in row and "code_salt" not in row


def test_the_live_code_never_reaches_twin(client):
    from adapter import otp
    client.post("/tools/send_otp", headers=HEADERS,
                json={"mc_number": "872144", "run_id": RUN})
    code = otp.peek("872144")["code"]            # what the device page shows
    assert code not in json.dumps(client.twin.writes)


def test_verify_mirrors_the_cleared_challenge(client):
    from adapter import otp
    client.post("/tools/send_otp", headers=HEADERS,
                json={"mc_number": "872144", "run_id": RUN})
    code = otp.peek("872144")["code"]
    r = client.post("/tools/verify_otp", headers=HEADERS,
                    json={"mc_number": "872144", "code": code, "run_id": RUN})
    assert r.get_json()["verified"] is True

    rows = client.twin.rows("insert", "otp_challenges")
    assert rows[-1][2]["verified"] is True
    assert rows[-1][2]["verified_at"]


def test_wrong_code_mirrors_the_attempt_count(client):
    client.post("/tools/send_otp", headers=HEADERS,
                json={"mc_number": "872144", "run_id": RUN})
    client.post("/tools/verify_otp", headers=HEADERS,
                json={"mc_number": "872144", "code": "000000", "run_id": RUN})
    row = client.twin.rows("insert", "otp_challenges")[-1][2]
    assert row["attempts"] == 1 and row["verified"] is False
