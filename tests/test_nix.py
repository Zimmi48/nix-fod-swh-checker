import json
import subprocess

import pytest

from nix_fod_swh_checker.models import FixedOutputDerivation
from nix_fod_swh_checker.nix import (
    NixCommandError,
    iter_fixed_output_derivations,
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
    assert flat_fod.urls == ["https://ftp.gnu.org/gnu/hello/hello-2.10.tar.gz"]
    assert flat_fod.label == "/nix/store/ccc-hello-2.10.tar.gz.drv"

    nar_fod = next(f for f in fods if f.method == "nar")
    assert nar_fod.urls == ["https://example.com/source.tar.gz"]


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


def test_show_derivations_recursive_parses_json(monkeypatch):
    def fake_run(cmd, check, capture_output, text):
        assert cmd[:4] == ["nix", "derivation", "show", "--recursive"]
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(SAMPLE_DERIVATIONS), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = show_derivations_recursive("nixpkgs#hello")
    assert result == SAMPLE_DERIVATIONS


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
