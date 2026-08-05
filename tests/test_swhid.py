import subprocess

import pytest

from nix_fod_swh_checker.swhid import SWHIdentifyError, compute_swhid


def test_compute_swhid_parses_output(monkeypatch):
    swhid = "swh:1:dir:" + "a" * 40

    def fake_run(cmd, check, capture_output, text):
        assert cmd == ["swh", "identify", "--no-filename", "/nix/store/x"]
        return subprocess.CompletedProcess(cmd, 0, stdout=f"{swhid}\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert compute_swhid("/nix/store/x") == swhid


def test_compute_swhid_missing_binary(monkeypatch):
    def fake_run(cmd, check, capture_output, text):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(SWHIdentifyError, match="could not find"):
        compute_swhid("/nix/store/x", swh_binary="does-not-exist")


def test_compute_swhid_command_error(monkeypatch):
    def fake_run(cmd, check, capture_output, text):
        raise subprocess.CalledProcessError(1, cmd, stderr="error: no such file")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(SWHIdentifyError, match="no such file"):
        compute_swhid("/nix/store/does-not-exist")


def test_compute_swhid_unexpected_output(monkeypatch):
    def fake_run(cmd, check, capture_output, text):
        return subprocess.CompletedProcess(cmd, 0, stdout="not a swhid\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(SWHIdentifyError, match="unexpected output"):
        compute_swhid("/nix/store/x")
