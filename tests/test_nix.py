import io
import json
import subprocess

import pytest

from nix_fod_swh_checker.models import FixedOutputDerivation
from nix_fod_swh_checker.nix import (
    NixCommandError,
    build_nix_file,
    dry_run_nix_file,
    iter_fixed_output_derivations,
    realise_fod,
    show_derivations_recursive,
)

SAMPLE_DERIVATIONS = {
    "/nix/store/aaa-hello-2.10.drv": {
        "name": "hello-2.10",
        "env": {},
        "outputs": {
            "out": {
                "path": "/nix/store/bbb-hello-2.10",
                "method": None,
                "hashAlgo": None,
                "hash": None,
            }
        },
    },
    "/nix/store/ccc-hello-2.10.tar.gz.drv": {
        "name": "hello-2.10.tar.gz",
        "env": {"urls": "https://ftp.gnu.org/gnu/hello/hello-2.10.tar.gz"},
        "outputs": {
            "out": {
                "path": "/nix/store/ddd-hello-2.10.tar.gz",
                "method": "flat",
                "hashAlgo": "sha256",
                "hash": "31e066137a962676e89f69d1b65382de95a7ef7d914b8cb956f41ea72e0f516",
            }
        },
    },
    "/nix/store/eee-some-source.drv": {
        "name": "some-source",
        "env": {"url": "https://example.com/source.tar.gz"},
        "outputs": {
            "out": {
                "path": "/nix/store/fff-some-source",
                "method": "nar",
                "hashAlgo": "sha256",
                "hash": "0000000000000000000000000000000000000000000000000000000000000",
            },
            "dev": {
                "path": None,
                "method": None,
                "hashAlgo": None,
                "hash": None,
            },
        },
    },
}


def test_iter_fixed_output_derivations_filters_non_fods():
    fods = list(iter_fixed_output_derivations(SAMPLE_DERIVATIONS))

    assert len(fods) == 2
    assert all(isinstance(fod, FixedOutputDerivation) for fod in fods)

    flat_fod = next(f for f in fods if f.method == "flat")
    assert flat_fod.hash_algo == "sha256"
    assert flat_fod.label == "/nix/store/ccc-hello-2.10.tar.gz.drv"

    nar_fod = next(f for f in fods if f.method == "nar")
    assert nar_fod.hash_algo == "sha256"


def test_iter_fixed_output_derivations_labels_non_out_output():
    derivations = {
        "/nix/store/x.drv": {
            "name": "x",
            "env": {},
            "outputs": {
                "dev": {
                    "path": "/nix/store/y-dev",
                    "method": "flat",
                    "hashAlgo": "sha256",
                    "hash": "aa" * 32,
                }
            },
        }
    }
    (fod,) = list(iter_fixed_output_derivations(derivations))
    assert fod.label == "/nix/store/x.drv^dev"


def test_iter_fixed_output_derivations_falls_back_to_output_hash_mode():
    # Many real-world `nix derivation show` outputs don't populate the
    # `method` field at all, only the legacy `outputHashMode` env var.
    derivations = {
        "/nix/store/flat.drv": {
            "name": "flat",
            "env": {"outputHashMode": "flat"},
            "outputs": {
                "out": {
                    "path": "/nix/store/flat-out",
                    "hashAlgo": "sha256",
                    "hash": "aa" * 32,
                }
            },
        },
        "/nix/store/recursive.drv": {
            "name": "recursive",
            "env": {"outputHashMode": "recursive"},
            "outputs": {
                "out": {
                    "path": "/nix/store/recursive-out",
                    "hashAlgo": "sha256",
                    "hash": "bb" * 32,
                }
            },
        },
    }
    flat_fod, recursive_fod = list(iter_fixed_output_derivations(derivations))
    assert flat_fod.method == "flat"
    assert recursive_fod.method == "nar"


def test_iter_fixed_output_derivations_ignores_non_derivation_metadata():
    derivations = {
        "version": 3,
        "/nix/store/fod.drv": {
            "name": "fod",
            "env": {},
            "outputs": {
                "out": {
                    "path": "/nix/store/fod-out",
                    "hashAlgo": "sha256",
                    "hash": "cc" * 32,
                }
            },
        },
    }

    (fod,) = list(iter_fixed_output_derivations(derivations))
    assert fod.drv_path == "/nix/store/fod.drv"
    assert fod.hash_hex == "cc" * 32


def test_show_derivations_recursive_parses_json(monkeypatch):
    def fake_run(cmd, check, capture_output, text):
        assert cmd[:4] == ["nix", "derivation", "show", "--recursive"]
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(SAMPLE_DERIVATIONS), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = show_derivations_recursive("nixpkgs#hello")
    assert result == SAMPLE_DERIVATIONS


def test_show_derivations_recursive_unwraps_derivations_key(monkeypatch):
    payload = {"version": 3, "derivations": SAMPLE_DERIVATIONS}

    def fake_run(cmd, check, capture_output, text):
        assert cmd[:4] == ["nix", "derivation", "show", "--recursive"]
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = show_derivations_recursive("nixpkgs#hello")
    assert result == SAMPLE_DERIVATIONS


def test_show_derivations_recursive_rejects_malformed_derivations_key(monkeypatch):
    payload = {"version": 3, "derivations": []}

    def fake_run(cmd, check, capture_output, text):
        assert cmd[:4] == ["nix", "derivation", "show", "--recursive"]
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(NixCommandError, match="'derivations' is not an object"):
        show_derivations_recursive("nixpkgs#hello")


def test_show_derivations_recursive_rejects_non_object_payload(monkeypatch):
    def fake_run(cmd, check, capture_output, text):
        assert cmd[:4] == ["nix", "derivation", "show", "--recursive"]
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(NixCommandError, match="top-level value is not an object"):
        show_derivations_recursive("nixpkgs#hello")


def test_show_derivations_recursive_rejects_flat_payload_without_drv_entries(monkeypatch):
    payload = {"version": 3}

    def fake_run(cmd, check, capture_output, text):
        assert cmd[:4] == ["nix", "derivation", "show", "--recursive"]
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(NixCommandError, match="no derivation entries found"):
        show_derivations_recursive("nixpkgs#hello")


def test_show_derivations_recursive_rejects_wrapped_payload_without_store_path_drv_entries(
    monkeypatch,
):
    payload = {
        "version": 3,
        "derivations": {
            "expr-strcmp.patch.drv": {"name": "expr-strcmp.patch", "env": {}, "outputs": {}}
        },
    }

    def fake_run(cmd, check, capture_output, text):
        assert cmd[:4] == ["nix", "derivation", "show", "--recursive"]
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(NixCommandError, match="no derivation entries found"):
        show_derivations_recursive("nixpkgs#hello")


def test_iter_fixed_output_derivations_skips_non_store_path_keys():
    derivations = {
        "version": 3,
        "expr-strcmp.patch.drv": {
            "name": "expr-strcmp.patch",
            "env": {"outputHashMode": "flat"},
            "outputs": {
                "out": {
                    "path": "/nix/store/x",
                    "hashAlgo": "sha256",
                    "hash": "aa" * 32,
                }
            },
        },
        "/nix/store/real.drv": {
            "name": "real",
            "env": {},
            "outputs": {
                "out": {
                    "path": "/nix/store/real-out",
                    "hashAlgo": "sha256",
                    "hash": "bb" * 32,
                }
            },
        },
    }

    (fod,) = list(iter_fixed_output_derivations(derivations))
    assert fod.drv_path == "/nix/store/real.drv"
    assert fod.hash_hex == "bb" * 32


def test_show_derivations_recursive_missing_binary(monkeypatch):
    def fake_run(cmd, check, capture_output, text):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(NixCommandError, match="could not find"):
        show_derivations_recursive("nixpkgs#hello", nix_binary="does-not-exist")


def test_show_derivations_recursive_command_error(monkeypatch):
    def fake_run(cmd, check, capture_output, text):
        raise subprocess.CalledProcessError(1, cmd, stderr="error: attribute not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(NixCommandError, match="attribute not found"):
        show_derivations_recursive("nixpkgs#doesNotExist")


def test_show_derivations_recursive_bad_json(monkeypatch):
    def fake_run(cmd, check, capture_output, text):
        return subprocess.CompletedProcess(cmd, 0, stdout="not json", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(NixCommandError, match="could not parse JSON"):
        show_derivations_recursive("nixpkgs#hello")


def _make_fod(**overrides):
    defaults = dict(
        drv_path="/nix/store/x.drv",
        output_name="out",
        output_path="/nix/store/y",
        name="x",
        method="nar",
        hash_algo="sha256",
        hash_hex="a" * 64,
    )
    defaults.update(overrides)
    return FixedOutputDerivation(**defaults)


def test_realise_fod_returns_out_path(monkeypatch):
    fod = _make_fod(drv_path="/nix/store/x.drv", output_name="out")

    def fake_run(cmd, check, capture_output, text):
        assert cmd[:4] == ["nix", "build", "--no-link", "--print-out-paths"]
        assert cmd[-1] == "/nix/store/x.drv^out"
        return subprocess.CompletedProcess(cmd, 0, stdout="/nix/store/y-out\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert realise_fod(fod) == "/nix/store/y-out"


def test_realise_fod_command_error(monkeypatch):
    fod = _make_fod()

    def fake_run(cmd, check, capture_output, text):
        raise subprocess.CalledProcessError(1, cmd, stderr="error: build failed")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(NixCommandError, match="build failed"):
        realise_fod(fod)


def test_realise_fod_no_output_paths(monkeypatch):
    fod = _make_fod()

    def fake_run(cmd, check, capture_output, text):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(NixCommandError, match="produced no output path"):
        realise_fod(fod)


def _fake_popen_factory(calls, returncode=0, stderr=""):
    def fake_popen(cmd, **kwargs):
        calls.append(cmd)

        class FakeProc:
            def __init__(self):
                self.returncode = returncode
                self._stderr = stderr

            @property
            def stderr(self):
                return io.StringIO(self._stderr)

            def communicate(self):
                return ("", self._stderr)

            def poll(self):
                return self.returncode

            def wait(self):
                return self.returncode

        return FakeProc()

    return fake_popen


def test_build_nix_file_runs_nix_build(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "Popen", _fake_popen_factory(calls))
    build_nix_file("/path/to/fods.nix")
    assert calls == [["nix", "build", "-f", "/path/to/fods.nix"]]


def test_build_nix_file_passes_extra_args(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "Popen", _fake_popen_factory(calls))
    build_nix_file("fods.nix", extra_args=["--rebuild", "--no-link"])
    assert calls == [["nix", "build", "-f", "fods.nix", "--rebuild", "--no-link"]]


def test_build_nix_file_command_error(monkeypatch):
    def fake_popen(cmd, **kwargs):
        class FakeProc:
            returncode = 1
            stderr = io.StringIO("error: build failed")

            def communicate(self):
                return ("", "error: build failed")

            def poll(self):
                return self.returncode

            def wait(self):
                return self.returncode

        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    with pytest.raises(NixCommandError, match="build failed"):
        build_nix_file("fods.nix")


def test_build_nix_file_with_attrs(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "Popen", _fake_popen_factory(calls))
    build_nix_file("fods.nix", attrs=["attr1", "attr2"])
    assert calls == [["nix", "build", "-f", "fods.nix", "attr1", "attr2"]]


def test_dry_run_nix_file_parses_json(monkeypatch):
    plan = [
        {
            "drvPath": "/nix/store/a.drv",
            "outputs": {"out": "/nix/store/a-out"},
        }
    ]

    def fake_run(cmd, check, capture_output, text):
        assert cmd[:5] == ["nix", "build", "--dry-run", "-f", "fods.nix"]
        assert "--json" in cmd
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(plan), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = dry_run_nix_file("fods.nix", ["attr1"])
    assert result == plan


def test_dry_run_nix_file_passes_no_substitute(monkeypatch):
    def fake_run(cmd, check, capture_output, text):
        assert "--no-substitute" in cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = dry_run_nix_file("fods.nix", no_substitute=True)
    assert result == []


def test_dry_run_nix_file_command_error(monkeypatch):
    def fake_run(cmd, check, capture_output, text):
        raise subprocess.CalledProcessError(1, cmd, stderr="error: dry run failed")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(NixCommandError, match="dry run failed"):
        dry_run_nix_file("fods.nix")
