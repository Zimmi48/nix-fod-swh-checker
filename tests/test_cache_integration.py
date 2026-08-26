import io
import tarfile
from pathlib import Path

import pytest

from nix_fod_swh_checker.cache import Cache
from nix_fod_swh_checker.disarchive import disassemble_archive
from nix_fod_swh_checker.models import FixedOutputDerivation
from nix_fod_swh_checker.swh import SWHClient
from nix_fod_swh_checker.swhid import compute_swhid


def _make_tar_archive(tmp_path, entries, suffix=".tar.gz"):
    archive = tmp_path / f"archive{suffix}"
    mode = "w:gz" if suffix.endswith(".gz") else "w"
    with tarfile.open(archive, mode) as tf:
        for name, content in entries:
            data = content.encode() if isinstance(content, str) else content
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return archive


class FakeResponse:
    def __init__(self, status_code, json_data=None):
        self.status_code = status_code
        self._json_data = json_data or {}

    def json(self):
        return self._json_data


def test_client_lookup_content_caches_known_result(monkeypatch, tmp_path):
    cache = Cache(tmp_path / "cache.json")
    client = SWHClient(min_delay=0, cache=cache)
    calls = []

    def capture(method, url, timeout, **kw):
        calls.append(url)
        return FakeResponse(200, {"checksums": {"sha1_git": "a" * 40}})

    monkeypatch.setattr(client.session, "request", capture)

    result1 = client.lookup_content("sha256", "abc")
    result2 = client.lookup_content("sha256", "abc")

    assert result1.known is True
    assert result2.known is True
    assert calls == ["https://archive.softwareheritage.org/api/1/content/sha256:abc/"]


def test_client_lookup_content_caches_miss_result(monkeypatch, tmp_path):
    cache = Cache(tmp_path / "cache.json")
    client = SWHClient(min_delay=0, cache=cache)
    calls = []

    def capture(method, url, timeout, **kw):
        calls.append(url)
        return FakeResponse(404)

    monkeypatch.setattr(client.session, "request", capture)

    result1 = client.lookup_content("sha256", "abc")
    result2 = client.lookup_content("sha256", "abc")

    assert result1.known is False
    assert result2.known is False
    assert len(calls) == 1


def test_client_lookup_content_ignores_miss_cache_when_requested(monkeypatch, tmp_path):
    cache = Cache(tmp_path / "cache.json", ignore_misses=True)
    client = SWHClient(min_delay=0, cache=cache)
    calls = []

    def capture(method, url, timeout, **kw):
        calls.append(url)
        return FakeResponse(404)

    monkeypatch.setattr(client.session, "request", capture)

    result1 = client.lookup_content("sha256", "abc")
    result2 = client.lookup_content("sha256", "abc")

    assert result1.known is False
    assert result2.known is False
    assert len(calls) == 2


def test_client_lookup_known_swhids_caches_per_swhid(monkeypatch, tmp_path):
    cache = Cache(tmp_path / "cache.json")
    client = SWHClient(min_delay=0, cache=cache)
    calls = []

    def capture(method, url, timeout, **kw):
        calls.append((method, kw.get("json")))
        return FakeResponse(200, {"swh:1:cnt:a": {"known": True}, "swh:1:cnt:b": {"known": False}})

    monkeypatch.setattr(client.session, "request", capture)

    result1 = client.lookup_known_swhids(["swh:1:cnt:a", "swh:1:cnt:b"])
    result2 = client.lookup_known_swhids(["swh:1:cnt:a", "swh:1:cnt:b"])

    assert result1 == {"swh:1:cnt:a": True, "swh:1:cnt:b": False}
    assert result2 == result1
    assert len(calls) == 1


def test_compute_swhid_caches_result(tmp_path):
    cache = Cache(tmp_path / "cache.json")
    path = tmp_path / "dir"
    path.mkdir()
    (path / "file.txt").write_text("hello")

    swhid1 = compute_swhid(str(path), cache=cache, cache_key=str(path))
    swhid2 = compute_swhid(str(path), cache=cache, cache_key=str(path))

    assert swhid1 == swhid2
    assert swhid1.startswith("swh:1:")


def test_disassemble_archive_caches_result(tmp_path):
    cache = Cache(tmp_path / "cache.json")
    archive = _make_tar_archive(tmp_path, [("src/file.txt", "hello")])

    spec1 = disassemble_archive(str(archive), cache=cache, cache_key=str(archive))
    spec2 = disassemble_archive(str(archive), cache=cache, cache_key=str(archive))

    assert spec1 == spec2
    assert "disarchive" in spec1


def test_disarchive_database_uses_cached_spec(monkeypatch, tmp_path):
    from nix_fod_swh_checker import disarchive as disarchive_module
    from nix_fod_swh_checker.disarchive import try_disarchive

    archive = _make_tar_archive(tmp_path, [("src/file.txt", "hello")])
    fod = FixedOutputDerivation(
        drv_path="/nix/store/x.drv",
        output_name="out",
        output_path="/nix/store/y",
        name="x",
        method="flat",
        hash_algo="sha256",
        hash_hex="a" * 64,
    )

    class FakeSWHClient:
        def __init__(self):
            self.known_calls = []

        def lookup_known_swhids(self, swhids):
            self.known_calls.append(list(swhids))
            return {swhid: False for swhid in swhids}

    client = FakeSWHClient()
    cache = Cache(tmp_path / "cache.json")

    # First call fetches (mocked) database and stores the spec in the cache.
    spec_captured = []

    class FakeResponse:
        status_code = 200
        text = "(disarchive (version 0) #f)"  # invalid, so treated as a miss

    monkeypatch.setattr(
        disarchive_module.requests, "get", lambda url, timeout=None: FakeResponse()
    )

    result1 = try_disarchive(
        fod,
        client,
        cache=cache,
        disarchive_db_url="https://example.com/disarchive",
    )
    assert result1 is None

    # Second call should use the cached miss and not query the database again.
    network_calls = []

    def fail_on_network(url, timeout=None):
        network_calls.append(url)
        raise AssertionError("should not query the database for a cached miss")

    monkeypatch.setattr(disarchive_module.requests, "get", fail_on_network)

    result2 = try_disarchive(
        fod,
        client,
        cache=cache,
        disarchive_db_url="https://example.com/disarchive",
    )
    assert result2 is None
    assert not network_calls
