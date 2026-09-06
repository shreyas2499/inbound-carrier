"""Twin's row API takes every value as a STRING, whatever the column type.

This was invisible for a long time: every writer in twin_helper is
fire-and-forget with errors swallowed, so the 400s went nowhere and the tables
simply stayed empty. The rule is pinned down here so a future refactor cannot
quietly reintroduce native JSON types.

Real error that produced these tests:
    400 Request doesn't match the schema:
        #/values/attempts/invalid_type:    expected string, received number
        #/values/verified/invalid_type:    expected string, received boolean
        #/values/verified_at/invalid_type: expected string, received null
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from adapter.twin_helper import TwinClient

wire = TwinClient._wire


def test_numbers_become_strings():
    assert wire({"attempts": 0, "latency_ms": 42, "rate": 2750}) == {
        "attempts": "0", "latency_ms": "42", "rate": "2750"}


def test_booleans_become_lowercase_strings_not_python_repr():
    """bool is a subclass of int in Python, so the bool branch has to come first
    or True would serialise as "1" -- or worse, str(True) == "True"."""
    assert wire({"verified": True, "eligible": False}) == {
        "verified": "true", "eligible": "false"}


def test_none_is_dropped_not_sent_as_null():
    """The API rejects nulls, and omission is also the semantics we want:
    'leave this column alone', not 'blank it'."""
    assert wire({"verified_at": None, "code": "123456"}) == {"code": "123456"}


def test_datetimes_become_iso_strings():
    moment = datetime(2026, 9, 6, 5, 28, 15, tzinfo=timezone.utc)
    assert wire({"created_at": moment})["created_at"].startswith("2026-09-06T05:28:15")


def test_dicts_and_lists_become_compact_json_for_jsonb():
    out = wire({"request": {"mc_number": "872144", "nested": {"n": 1}},
                "tags": ["a", "b"]})
    assert json.loads(out["request"]) == {"mc_number": "872144", "nested": {"n": 1}}
    assert json.loads(out["tags"]) == ["a", "b"]
    assert " " not in out["request"], "compact separators, no wasted bytes"


def test_strings_pass_through_untouched():
    assert wire({"mc_number": "872144"}) == {"mc_number": "872144"}


def test_every_value_out_is_a_string():
    """The invariant, stated directly: whatever goes in, strings come out."""
    mixed = {"a": 1, "b": True, "c": None, "d": {"x": 1}, "e": "s",
             "f": 1.5, "g": datetime.now(tz=timezone.utc), "h": [1, 2]}
    assert all(isinstance(v, str) for v in wire(mixed).values())


def test_the_exact_payload_that_produced_the_400():
    """otp.issue's insert, which Twin rejected on three fields at once."""
    out = wire({
        "mc_number": "872144",
        "code": "123456",
        "created_at": "2026-09-06T05:28:15+00:00",
        "expires_at": "2026-09-06T05:31:15+00:00",
        "attempts": 0,
        "verified": False,
        "verified_at": None,
        "run_id": "2d334ac5-8fb1-472b-89c0-c7bd15f9cb8b",
    })
    assert out["attempts"] == "0"
    assert out["verified"] == "false"
    assert "verified_at" not in out
    assert all(isinstance(v, str) for v in out.values())


def test_insert_and_update_both_coerce():
    """The coercion lives in the client, so every caller gets it -- including the
    primary key on a PATCH."""
    sent = {}

    class Recording(TwinClient):
        def __init__(self): pass
        def _request(self, method, path, *, json_body=None, params=None):
            sent[method] = json_body
            return None

    c = Recording()
    c.insert_row("otp_challenges", {"attempts": 0, "verified": True})
    c.update_row("call_records", {"run_id": "r1"}, {"loadboard_rate": 2600})
    assert sent["POST"] == {"values": {"attempts": "0", "verified": "true"}}
    assert sent["PATCH"] == {"primaryKey": {"run_id": "r1"},
                             "updates": {"loadboard_rate": "2600"}}
