import shutil
import subprocess
from pathlib import Path

import pytest

from nix_fod_swh_checker.swhid import SWHIdentifyError, compute_swhid


# Canonical SWHID for a file containing the bytes ``b"hello"`` as computed by
# the reference ``swh identify --no-filename`` tool from swh-model.
KNOWN_CONTENT_SWHID = "swh:1:cnt:b6fc4c620b67d95f953a5c1c1230aaab5db5a1b0"


def test_compute_swhid_for_file(tmp_path):
    file_path = tmp_path / "file.txt"
    file_path.write_text("hello")

    result = compute_swhid(str(file_path))

    assert result == KNOWN_CONTENT_SWHID


def test_compute_swhid_for_directory(tmp_path):
    (tmp_path / "a").write_text("a")
    (tmp_path / "b").write_text("b")

    result = compute_swhid(str(tmp_path))

    assert result.startswith("swh:1:dir:")


def test_compute_swhid_missing_binary(tmp_path):
    file_path = tmp_path / "file.txt"
    file_path.write_text("hello")

    with pytest.raises(SWHIdentifyError, match="could not find"):
        compute_swhid(str(file_path), swh_binary="does-not-exist")


def test_compute_swhid_command_error(tmp_path):
    with pytest.raises(SWHIdentifyError, match="failed with exit code"):
        compute_swhid("/nix/store/does-not-exist")


def test_compute_swhid_unexpected_output(monkeypatch):
    def fake_run(cmd, check, capture_output, text, timeout=None):
        return subprocess.CompletedProcess(cmd, 0, stdout="not a swhid\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(SWHIdentifyError, match="unexpected output"):
        compute_swhid("/nix/store/x")


def test_compute_swhid_timeout(monkeypatch):
    def fake_run(cmd, check, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(SWHIdentifyError, match="timed out after 1.0s"):
        compute_swhid("/nix/store/x", timeout=1.0)


def test_compute_swhid_caches_timeout_and_reuses_it(monkeypatch, tmp_path):
    from nix_fod_swh_checker.cache import Cache

    calls = []

    def fake_run(cmd, check, capture_output, text, timeout):
        calls.append(timeout)
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(subprocess, "run", fake_run)
    cache = Cache(tmp_path / "cache.json")

    with pytest.raises(SWHIdentifyError, match="timed out after 1.0s"):
        compute_swhid("/nix/store/x", timeout=1.0, cache=cache, cache_key="k")
    assert len(calls) == 1

    cache.save()
    cache2 = Cache(tmp_path / "cache.json")
    # A lower timeout reuses the cached timeout miss.
    with pytest.raises(SWHIdentifyError, match="timed out after 1.0s"):
        compute_swhid("/nix/store/x", timeout=0.5, cache=cache2, cache_key="k")
    assert len(calls) == 1

    # An equal timeout also reuses the cached timeout miss.
    with pytest.raises(SWHIdentifyError, match="timed out after 1.0s"):
        compute_swhid("/nix/store/x", timeout=1.0, cache=cache2, cache_key="k")
    assert len(calls) == 1


def test_compute_swhid_ignores_cached_timeout_when_timeout_increased(monkeypatch, tmp_path):
    from nix_fod_swh_checker.cache import Cache

    calls = []

    def fake_run(cmd, check, capture_output, text, timeout):
        calls.append(timeout)
        if timeout <= 1.0:
            raise subprocess.TimeoutExpired(cmd, timeout)
        return subprocess.CompletedProcess(cmd, 0, stdout=f"{KNOWN_CONTENT_SWHID}\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    cache = Cache(tmp_path / "cache.json")

    with pytest.raises(SWHIdentifyError, match="timed out after 1.0s"):
        compute_swhid("/nix/store/x", timeout=1.0, cache=cache, cache_key="k")
    assert len(calls) == 1

    cache.save()
    cache2 = Cache(tmp_path / "cache.json")
    result = compute_swhid("/nix/store/x", timeout=2.0, cache=cache2, cache_key="k")
    assert result == KNOWN_CONTENT_SWHID
    assert len(calls) == 2


def test_compute_swhid_retry_flags_ignore_cached_timeout(monkeypatch, tmp_path):
    from nix_fod_swh_checker.cache import Cache

    calls = []

    def fake_run(cmd, check, capture_output, text, timeout):
        calls.append(timeout)
        # First call times out; subsequent calls (retry) succeed.
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd, timeout)
        return subprocess.CompletedProcess(cmd, 0, stdout=f"{KNOWN_CONTENT_SWHID}\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    cache = Cache(tmp_path / "cache.json")

    with pytest.raises(SWHIdentifyError, match="timed out after 1.0s"):
        compute_swhid("/nix/store/x", timeout=1.0, cache=cache, cache_key="k")
    assert len(calls) == 1

    cache.save()
    cache2 = Cache(tmp_path / "cache.json", ignore_misses=True)
    result = compute_swhid("/nix/store/x", timeout=0.5, cache=cache2, cache_key="k")
    assert result == KNOWN_CONTENT_SWHID
    assert len(calls) == 2


def test_compute_swhid_invokes_configured_binary(tmp_path, monkeypatch):
    """The wrapper passes ``swh_binary`` through to the subprocess unchanged."""
    recorded = []

    def fake_run(cmd, check, capture_output, text, timeout=None):
        recorded.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=f"{KNOWN_CONTENT_SWHID}\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = compute_swhid(str(tmp_path), swh_binary="my-swh")
    assert result == KNOWN_CONTENT_SWHID
    assert recorded == [["my-swh", "identify", "--no-filename", str(tmp_path)]]
