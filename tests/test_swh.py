import pytest
import requests

from nix_fod_swh_checker.swh import SWHClient, SWHError


class FakeResponse:
    def __init__(self, status_code, json_data=None, headers=None):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.headers = headers or {}

    def json(self):
        return self._json_data


def test_lookup_content_known(monkeypatch):
    client = SWHClient(min_delay=0)
    monkeypatch.setattr(
        client.session,
        "request",
        lambda method, url, timeout, **kw: FakeResponse(200, {"checksums": {"sha256": "abc"}}),
    )
    result = client.lookup_content("sha256", "abc")
    assert result.known is True
    assert result.raw == {"checksums": {"sha256": "abc"}}


def test_lookup_content_not_known(monkeypatch):
    client = SWHClient(min_delay=0)
    monkeypatch.setattr(
        client.session, "request", lambda method, url, timeout, **kw: FakeResponse(404)
    )
    result = client.lookup_content("sha256", "abc")
    assert result.known is False


def test_lookup_content_rejects_unsupported_algo():
    client = SWHClient(min_delay=0)
    with pytest.raises(ValueError):
        client.lookup_content("md5", "abc")


def test_lookup_known_swhids(monkeypatch):
    client = SWHClient(min_delay=0)
    swhid = "swh:1:cnt:" + "a" * 40
    monkeypatch.setattr(
        client.session,
        "request",
        lambda method, url, timeout, **kw: FakeResponse(200, {swhid: {"known": True}}),
    )
    result = client.lookup_known_swhids([swhid])
    assert result == {swhid: True}


def test_lookup_known_swhids_empty_list_short_circuits(monkeypatch):
    client = SWHClient(min_delay=0)

    def fail(*args, **kwargs):
        raise AssertionError("should not make a request for an empty list")

    monkeypatch.setattr(client.session, "request", fail)
    assert client.lookup_known_swhids([]) == {}


def test_lookup_origin(monkeypatch):
    client = SWHClient(min_delay=0)
    monkeypatch.setattr(
        client.session, "request", lambda method, url, timeout, **kw: FakeResponse(200, {})
    )
    assert client.lookup_origin("https://example.com/x.tar.gz") is True


def test_request_retries_on_429_then_succeeds(monkeypatch):
    client = SWHClient(min_delay=0, max_retries=2)
    responses = [FakeResponse(429, headers={"Retry-After": "0"}), FakeResponse(200, {})]
    monkeypatch.setattr(
        client.session, "request", lambda method, url, timeout, **kw: responses.pop(0)
    )
    monkeypatch.setattr("nix_fod_swh_checker.swh.time.sleep", lambda *_: None)
    assert client.lookup_origin("https://example.com/x.tar.gz") is True


def test_request_raises_swherror_after_exhausting_retries(monkeypatch):
    client = SWHClient(min_delay=0, max_retries=1)

    def always_fail(method, url, timeout, **kw):
        raise requests.ConnectionError("boom")

    monkeypatch.setattr(client.session, "request", always_fail)
    monkeypatch.setattr("nix_fod_swh_checker.swh.time.sleep", lambda *_: None)
    with pytest.raises(SWHError):
        client.lookup_origin("https://example.com/x.tar.gz")
