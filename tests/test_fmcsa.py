"""Tests for the FMCSA authority lookup. The HTTP call is faked so we exercise
every branch — eligible, not-allowed, out-of-service, not-found, and API-down —
without hitting the real FMCSA API.
"""
import pytest
import requests

from adapter import fmcsa


class FakeResp:
    def __init__(self, json_data, status=200):
        self._json = json_data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self._json


def _patch(monkeypatch, *, resp=None, exc=None):
    def fake_get(url, params=None, timeout=None):
        if exc:
            raise exc
        return resp
    monkeypatch.setattr(fmcsa.requests, "get", fake_get)


def _carrier(**fields):
    return FakeResp({"content": [{"carrier": fields}]})


def test_active_carrier_is_eligible(monkeypatch):
    _patch(monkeypatch, resp=_carrier(
        allowedToOperate="Y", outOfService="N", legalName="ACME TRUCKING LLC",
        dotNumber=1234567, telephone="5551234567"))
    r = fmcsa.verify_mc("MC-872144", "key")
    assert r["found"] and r["eligible"]
    assert r["legal_name"] == "ACME TRUCKING LLC"
    assert r["phone"] == "5551234567"
    assert r["mc_number"] == "872144"          # non-digits stripped


def test_not_allowed_to_operate_is_ineligible(monkeypatch):
    _patch(monkeypatch, resp=_carrier(allowedToOperate="N", legalName="X"))
    r = fmcsa.verify_mc("872144", "key")
    assert r["found"] and not r["eligible"]


def test_out_of_service_is_ineligible(monkeypatch):
    _patch(monkeypatch, resp=_carrier(allowedToOperate="Y", outOfService="Y", legalName="X"))
    assert fmcsa.verify_mc("872144", "key")["eligible"] is False


def test_carrier_not_found(monkeypatch):
    _patch(monkeypatch, resp=FakeResp({"content": None}))
    r = fmcsa.verify_mc("9999999", "key")
    assert not r["found"] and not r["eligible"]


def test_blank_mc_number(monkeypatch):
    r = fmcsa.verify_mc("", "key")
    assert not r["found"] and "no MC number" in r["reason"]


def test_api_down_raises(monkeypatch):
    _patch(monkeypatch, exc=requests.ConnectionError("boom"))
    with pytest.raises(fmcsa.FmcsaUnavailable):
        fmcsa.verify_mc("872144", "key")
