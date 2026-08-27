import json
import time

import pytest

from nix_archive_src.cache import Cache, default_cache_path, DEFAULT_MISS_TTL_SECONDS


def test_default_cache_path_uses_xdg_cache_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    path = default_cache_path()
    assert path == tmp_path / "nix-archive-src" / "cache.json"


def test_default_cache_path_falls_back_to_home(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    path = default_cache_path()
    assert path == tmp_path / ".cache" / "nix-archive-src" / "cache.json"


def test_cache_hit_roundtrip(tmp_path):
    cache = Cache(tmp_path / "cache.json")
    cache.set("key", {"value": 42}, is_miss=False)
    cache.save()

    loaded = Cache(tmp_path / "cache.json")
    assert loaded.get("key") == {"value": 42}


def test_cache_miss_roundtrip(tmp_path):
    cache = Cache(tmp_path / "cache.json")
    cache.set("key", {"known": False}, is_miss=True)
    cache.save()

    loaded = Cache(tmp_path / "cache.json")
    assert loaded.get("key") == {"known": False}


def test_cache_miss_expires_after_ttl(tmp_path, monkeypatch):
    cache = Cache(tmp_path / "cache.json")
    cache.set("key", {"known": False}, is_miss=True)
    cache.save()

    future = time.time() + DEFAULT_MISS_TTL_SECONDS + 1
    monkeypatch.setattr(
        "nix_fod_swh_checker.cache.time.time",
        lambda: future,
    )
    loaded = Cache(tmp_path / "cache.json")
    assert loaded.get("key") is None


def test_cache_ignore_misses_drops_miss_entries(tmp_path):
    cache = Cache(tmp_path / "cache.json")
    cache.set("hit", {"known": True}, is_miss=False)
    cache.set("miss", {"known": False}, is_miss=True)
    cache.save()

    loaded = Cache(tmp_path / "cache.json", ignore_misses=True)
    assert loaded.get("hit") == {"known": True}
    assert loaded.get("miss") is None


def test_cache_get_removes_expired_miss(tmp_path, monkeypatch):
    cache = Cache(tmp_path / "cache.json")
    cache.set("key", {"known": False}, is_miss=True)

    future = time.time() + DEFAULT_MISS_TTL_SECONDS + 1
    monkeypatch.setattr(
        "nix_fod_swh_checker.cache.time.time",
        lambda: future,
    )
    assert cache.get("key") is None
    assert "key" not in cache._entries
    assert cache._dirty is True


def test_cache_delete(tmp_path):
    cache = Cache(tmp_path / "cache.json")
    cache.set("key", {"value": 1}, is_miss=False)
    cache.delete("key")
    assert cache.get("key") is None


def test_cache_save_only_when_dirty(tmp_path):
    cache = Cache(tmp_path / "cache.json")
    cache.save()
    assert not (tmp_path / "cache.json").exists()

    cache.set("key", {"value": 1}, is_miss=False)
    cache.save()
    assert (tmp_path / "cache.json").exists()

    mtime = (tmp_path / "cache.json").stat().st_mtime
    cache.save()
    assert (tmp_path / "cache.json").stat().st_mtime == mtime


def test_cache_corrupt_file_treated_as_empty(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text("not json")
    cache = Cache(path)
    assert cache.get("key") is None


def test_cache_unknown_version_treated_as_empty(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"version": 999, "entries": {"key": {"kind": "hit"}}}))
    cache = Cache(path)
    assert cache.get("key") is None


def test_cache_invalid_entry_skipped(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {
                    "good": {"kind": "hit", "value": {"x": 1}},
                    "bad": {"kind": "miss"},  # missing created timestamp
                },
            }
        )
    )
    cache = Cache(path)
    assert cache.get("good") == {"x": 1}
    assert cache.get("bad") is None


def test_cache_get_tool_result_reuses_hits_regardless_of_timeout(tmp_path):
    cache = Cache(tmp_path / "cache.json")
    cache.set("key", {"swhid": "swh:1:dir:abc", "timeout": 10.0}, is_miss=False)
    assert cache.get_tool_result("key", timeout=5.0) == {
        "swhid": "swh:1:dir:abc",
        "timeout": 10.0,
    }
    assert cache.get_tool_result("key", timeout=20.0) == {
        "swhid": "swh:1:dir:abc",
        "timeout": 10.0,
    }


def test_cache_get_tool_result_reuses_timeout_when_current_is_lower_or_equal(tmp_path):
    cache = Cache(tmp_path / "cache.json")
    cache.set("key", {"timed_out": True, "timeout": 10.0}, is_miss=True)
    assert cache.get_tool_result("key", timeout=5.0) == {
        "timed_out": True,
        "timeout": 10.0,
    }
    assert cache.get_tool_result("key", timeout=10.0) == {
        "timed_out": True,
        "timeout": 10.0,
    }


def test_cache_get_tool_result_ignores_timeout_when_current_is_higher(tmp_path):
    cache = Cache(tmp_path / "cache.json")
    cache.set("key", {"timed_out": True, "timeout": 10.0}, is_miss=True)
    assert cache.get_tool_result("key", timeout=20.0) is None


def test_cache_get_tool_result_ignores_timeout_miss_when_ignore_misses(tmp_path):
    cache = Cache(tmp_path / "cache.json", ignore_misses=True)
    cache.set("key", {"timed_out": True, "timeout": 10.0}, is_miss=True)
    assert cache.get_tool_result("key", timeout=5.0) is None
