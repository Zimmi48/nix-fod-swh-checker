import pytest
import requests

from nix_archive_src.swh import SWHClient, SWHError, VaultCookingError


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


def test_request_retries_on_429_then_succeeds(monkeypatch):
    client = SWHClient(min_delay=0, max_retries=2)
    responses = [FakeResponse(429, headers={"Retry-After": "0"}), FakeResponse(200, {})]
    monkeypatch.setattr(
        client.session, "request", lambda method, url, timeout, **kw: responses.pop(0)
    )
    monkeypatch.setattr("nix_fod_swh_checker.swh.time.sleep", lambda *_: None)
    result = client.lookup_content("sha256", "abc")
    assert result.known is True


def test_request_retries_using_x_ratelimit_reset(monkeypatch):
    # The real Software Heritage API does not send `Retry-After` on 429s,
    # only `X-RateLimit-Reset` (a Unix timestamp), confirmed by inspecting a
    # live response.
    monkeypatch.setattr("nix_fod_swh_checker.swh.time.time", lambda: 1000.0)
    client = SWHClient(min_delay=0, max_retries=2)
    responses = [
        FakeResponse(429, headers={"X-RateLimit-Reset": "1010"}),
        FakeResponse(200, {}),
    ]
    monkeypatch.setattr(
        client.session, "request", lambda method, url, timeout, **kw: responses.pop(0)
    )
    sleeps = []
    monkeypatch.setattr("nix_fod_swh_checker.swh.time.sleep", lambda s: sleeps.append(s))
    result = client.lookup_content("sha256", "abc")
    assert result.known is True
    assert sleeps == [10.0]


def test_warn_if_quota_low_logs_when_remaining_is_low(monkeypatch):
    monkeypatch.setattr("nix_fod_swh_checker.swh.time.time", lambda: 1000.0)
    messages = []
    client = SWHClient(min_delay=0, on_log=messages.append)
    monkeypatch.setattr(
        client.session,
        "request",
        lambda method, url, timeout, **kw: FakeResponse(
            200, {}, headers={"X-RateLimit-Remaining": "2", "X-RateLimit-Reset": "1030"}
        ),
    )
    client.lookup_content("sha256", "abc")
    assert any("quota running low" in m for m in messages)


def test_warn_if_quota_low_silent_when_remaining_is_high(monkeypatch):
    messages = []
    client = SWHClient(min_delay=0, on_log=messages.append)
    monkeypatch.setattr(
        client.session,
        "request",
        lambda method, url, timeout, **kw: FakeResponse(
            200, {}, headers={"X-RateLimit-Remaining": "100"}
        ),
    )
    client.lookup_content("sha256", "abc")
    assert not any("quota running low" in m for m in messages)


def test_request_raises_swherror_after_exhausting_retries(monkeypatch):
    client = SWHClient(min_delay=0, max_retries=1)

    def always_fail(method, url, timeout, **kw):
        raise requests.ConnectionError("boom")

    monkeypatch.setattr(client.session, "request", always_fail)
    monkeypatch.setattr("nix_fod_swh_checker.swh.time.sleep", lambda *_: None)
    with pytest.raises(SWHError):
        client.lookup_content("sha256", "abc")


def _vault_task_json(swhid, status="pending", fetch_url=None):
    return {
        "id": 123,
        "swhid": swhid,
        "status": status,
        "progress_message": "cooking",
        "fetch_url": fetch_url,
    }


def test_cook_vault_flat_posts_and_returns_task(monkeypatch):
    swhid = "swh:1:dir:" + "a" * 40
    client = SWHClient(min_delay=0)
    monkeypatch.setattr(
        client.session,
        "request",
        lambda method, url, timeout, **kw: (
            FakeResponse(201, _vault_task_json(swhid)) if method == "POST" else FakeResponse(404)
        ),
    )
    task = client.cook_vault_flat(swhid)
    assert task.id == 123
    assert task.swhid == swhid
    assert task.status == "pending"


def test_get_vault_flat_task_returns_task(monkeypatch):
    swhid = "swh:1:dir:" + "a" * 40
    client = SWHClient(min_delay=0)
    monkeypatch.setattr(
        client.session,
        "request",
        lambda method, url, timeout, **kw: FakeResponse(200, _vault_task_json(swhid, status="done")),
    )
    task = client.get_vault_flat_task(swhid)
    assert task.status == "done"


def test_get_vault_flat_task_404_returns_none(monkeypatch):
    swhid = "swh:1:dir:" + "a" * 40
    client = SWHClient(min_delay=0)
    monkeypatch.setattr(client.session, "request", lambda method, url, timeout, **kw: FakeResponse(404))
    assert client.get_vault_flat_task(swhid) is None


def test_ensure_vault_flat_cooking_checks_existing_task_before_posting(monkeypatch):
    swhid = "swh:1:dir:" + "a" * 40
    client = SWHClient(min_delay=0)
    responses = [
        FakeResponse(200, _vault_task_json(swhid, status="pending")),
    ]
    monkeypatch.setattr(client.session, "request", lambda method, url, timeout, **kw: responses.pop(0))
    task = client.ensure_vault_flat_cooking(swhid)
    assert task.status == "pending"


def test_ensure_vault_flat_cooking_posts_when_no_task_exists(monkeypatch):
    swhid = "swh:1:dir:" + "a" * 40
    client = SWHClient(min_delay=0)
    responses = [
        FakeResponse(404),
        FakeResponse(201, _vault_task_json(swhid, status="new")),
    ]
    monkeypatch.setattr(client.session, "request", lambda method, url, timeout, **kw: responses.pop(0))
    task = client.ensure_vault_flat_cooking(swhid)
    assert task.status == "new"


def test_wait_for_vault_flat_polls_until_done(monkeypatch):
    swhid = "swh:1:dir:" + "a" * 40
    client = SWHClient(min_delay=0)
    responses = [
        FakeResponse(404),
        FakeResponse(201, _vault_task_json(swhid, status="new")),
        FakeResponse(200, _vault_task_json(swhid, status="pending")),
        FakeResponse(200, _vault_task_json(swhid, status="done", fetch_url="https://example.com")),
    ]
    monkeypatch.setattr(client.session, "request", lambda method, url, timeout, **kw: responses.pop(0))
    monkeypatch.setattr("nix_fod_swh_checker.swh.time.sleep", lambda *_: None)
    task = client.wait_for_vault_flat(swhid, poll_interval=0.01)
    assert task.status == "done"
    assert task.fetch_url == "https://example.com"


def test_wait_for_vault_flat_raises_on_failed_task(monkeypatch):
    swhid = "swh:1:dir:" + "a" * 40
    client = SWHClient(min_delay=0)
    responses = [
        FakeResponse(200, _vault_task_json(swhid, status="failed")),
    ]
    monkeypatch.setattr(client.session, "request", lambda method, url, timeout, **kw: responses.pop(0))
    monkeypatch.setattr("nix_fod_swh_checker.swh.time.sleep", lambda *_: None)
    with pytest.raises(VaultCookingError, match="failed"):
        client.wait_for_vault_flat(swhid)


def test_wait_for_vault_flat_raises_on_timeout(monkeypatch):
    swhid = "swh:1:dir:" + "a" * 40
    client = SWHClient(min_delay=0)
    responses = [
        FakeResponse(200, _vault_task_json(swhid, status="pending")),
    ]
    monkeypatch.setattr(client.session, "request", lambda method, url, timeout, **kw: responses.pop(0))
    monkeypatch.setattr("nix_fod_swh_checker.swh.time.sleep", lambda *_: None)
    with pytest.raises(VaultCookingError, match="timed out"):
        client.wait_for_vault_flat(swhid, timeout=0.0)


def _save_request_json(
    request_id=42,
    origin_url="https://example.com/repo.git",
    visit_type="git",
    save_request_status="accepted",
    save_task_status="pending",
):
    return {
        "id": request_id,
        "request_url": f"https://archive.softwareheritage.org/api/1/origin/save/{request_id}/",
        "origin_url": origin_url,
        "visit_type": visit_type,
        "save_request_date": "2026-08-13T00:00:00Z",
        "save_request_status": save_request_status,
        "save_task_status": save_task_status,
        "visit_date": None,
        "visit_status": None,
        "note": None,
        "snapshot_swhid": None,
        "snapshot_url": None,
        "from_webhook": False,
        "webhook_origin": None,
    }


def test_request_origin_save_posts_with_query_params(monkeypatch):
    client = SWHClient(min_delay=0)
    calls = []

    def capture(method, url, timeout, **kw):
        calls.append((method, url, kw.get("params")))
        return FakeResponse(201, _save_request_json())

    monkeypatch.setattr(client.session, "request", capture)
    request = client.request_origin_save("https://example.com/repo.git", visit_type="git")
    assert request.origin_url == "https://example.com/repo.git"
    assert request.visit_type == "git"
    assert request.save_request_status == "accepted"
    assert calls == [("POST", "https://archive.softwareheritage.org/api/1/origin/save/", {"origin_url": "https://example.com/repo.git", "visit_type": "git"})]


def test_request_origin_save_defaults_to_tarball(monkeypatch):
    client = SWHClient(min_delay=0)

    def capture(method, url, timeout, **kw):
        return FakeResponse(201, _save_request_json(visit_type="tarball", save_task_status="not created"))

    monkeypatch.setattr(client.session, "request", capture)
    request = client.request_origin_save("https://example.com/archive.tar.gz")
    assert request.visit_type == "tarball"


def test_request_origin_save_raises_on_blocked_origin(monkeypatch):
    client = SWHClient(min_delay=0)
    monkeypatch.setattr(
        client.session,
        "request",
        lambda method, url, timeout, **kw: FakeResponse(403),
    )
    with pytest.raises(SWHError, match="blocked"):
        client.request_origin_save("https://blocked.example.com/")


def test_get_origin_save_request_returns_status(monkeypatch):
    client = SWHClient(min_delay=0)
    monkeypatch.setattr(
        client.session,
        "request",
        lambda method, url, timeout, **kw: FakeResponse(200, _save_request_json(request_id=7, save_task_status="succeeded")),
    )
    request = client.get_origin_save_request(7)
    assert request.id == 7
    assert request.save_task_status == "succeeded"
