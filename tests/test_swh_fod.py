"""Tests for SWH-backed FOD expression generation."""
from __future__ import annotations

import pytest

from nix_fod_swh_checker.models import FixedOutputDerivation, SWHCheckResult, SWHLookupMethod
from nix_fod_swh_checker.swh_fod import (
    SWHFodExpression,
    swh_fod_expression,
    swh_fods_expression,
    vault_swhids_for_results,
    write_swh_fods_nix,
)


def make_fod(**overrides):
    defaults = dict(
        drv_path="/nix/store/x.drv",
        output_name="out",
        output_path="/nix/store/y",
        name="x",
        method="flat",
        hash_algo="sha256",
        hash_hex="a" * 64,
    )
    defaults.update(overrides)
    return FixedOutputDerivation(**defaults)


def make_result(**overrides):
    defaults = dict(
        fod=make_fod(),
        known=True,
        method=SWHLookupMethod.CONTENT_HASH,
        detail="test",
        swhid=None,
        swh_url=None,
        disarchive_spec=None,
        disarchive_swhid=None,
        disarchive_top_dir=None,
    )
    defaults.update(overrides)
    return SWHCheckResult(**defaults)


def test_content_hash_expression():
    result = make_result(
        method=SWHLookupMethod.CONTENT_HASH,
        swhid="swh:1:cnt:" + "b" * 40,
    )
    expr = swh_fod_expression(result)
    assert isinstance(expr, SWHFodExpression)
    assert "builtin:fetchurl" in expr.nix_code
    assert "outputHashMode = \"flat\"" in expr.nix_code
    assert "archive.softwareheritage.org/api/1/content/sha256:" + "a" * 64 in expr.nix_code


def test_content_swhid_expression():
    result = make_result(
        fod=make_fod(method="git", hash_algo="sha1", hash_hex="b" * 40),
        method=SWHLookupMethod.SWHID_KNOWN,
        swhid="swh:1:cnt:" + "b" * 40,
    )
    expr = swh_fod_expression(result)
    assert isinstance(expr, SWHFodExpression)
    assert "builtin:fetchurl" in expr.nix_code
    assert "outputHashMode = \"flat\"" in expr.nix_code
    assert "archive.softwareheritage.org/api/1/content/sha1_git:" + "b" * 40 in expr.nix_code


def test_directory_swhid_expression():
    swhid = "swh:1:dir:" + "b" * 40
    result = make_result(
        fod=make_fod(method="nar", hash_algo="sha256", hash_hex="a" * 64),
        method=SWHLookupMethod.SWHID_KNOWN,
        swhid=swhid,
    )
    expr = swh_fod_expression(result)
    assert isinstance(expr, SWHFodExpression)
    assert "pkgs.stdenv.mkDerivation" in expr.nix_code
    assert "outputHashMode = \"recursive\"" in expr.nix_code
    assert f"vault/flat/{swhid}/raw" in expr.nix_code
    assert "curl -L -f -o tmp/bundle.tar.bz2" in expr.nix_code
    assert "tar -xjf tmp/bundle.tar.bz2" in expr.nix_code


def test_build_and_identify_directory_expression():
    swhid = "swh:1:dir:" + "c" * 40
    result = make_result(
        fod=make_fod(method="nar", hash_algo="sha256", hash_hex="a" * 64),
        method=SWHLookupMethod.BUILD_AND_IDENTIFY,
        swhid=swhid,
    )
    expr = swh_fod_expression(result)
    assert isinstance(expr, SWHFodExpression)
    assert "pkgs.stdenv.mkDerivation" in expr.nix_code
    assert "outputHashMode = \"recursive\"" in expr.nix_code
    assert f"vault/flat/{swhid}/raw" in expr.nix_code
    assert "curl -L -f -o tmp/bundle.tar.bz2" in expr.nix_code
    assert "tar -xjf tmp/bundle.tar.bz2" in expr.nix_code


def test_disarchive_expression_requires_spec():
    swhid = "swh:1:dir:" + "d" * 40
    result = make_result(
        fod=make_fod(method="flat", hash_algo="sha256", hash_hex="a" * 64),
        method=SWHLookupMethod.KNOWN_AFTER_DISARCHIVE,
        swhid=swhid,
        disarchive_spec=None,
    )
    assert swh_fod_expression(result) is None


def test_disarchive_expression_with_direct_swhid():
    swhid = "swh:1:dir:" + "d" * 40
    result = make_result(
        fod=make_fod(method="flat", hash_algo="sha256", hash_hex="a" * 64),
        method=SWHLookupMethod.KNOWN_AFTER_DISARCHIVE,
        swhid=swhid,
        disarchive_swhid=swhid,
        disarchive_spec="(disarchive (version 0))",
    )
    expr = swh_fod_expression(result)
    assert isinstance(expr, SWHFodExpression)
    assert "outputHashMode = \"flat\"" in expr.nix_code
    assert "pkgs.stdenv.mkDerivation" in expr.nix_code
    assert "builtins.toFile \"disarchive.spec\"" in expr.nix_code
    assert "disarchive assemble" in expr.nix_code
    assert "curl -L -f -o tmp/bundle.tar.bz2" in expr.nix_code
    assert "tar -xjf tmp/bundle.tar.bz2" in expr.nix_code
    assert "(disarchive (version 0))" in expr.nix_code


def test_disarchive_expression_with_wrapped_stripped_swhid():
    stripped = "swh:1:dir:" + "d" * 40
    disarchive_swhid = "swh:1:dir:" + "e" * 40
    result = make_result(
        fod=make_fod(method="flat", hash_algo="sha256", hash_hex="a" * 64),
        method=SWHLookupMethod.KNOWN_AFTER_DISARCHIVE,
        swhid=stripped,
        disarchive_swhid=disarchive_swhid,
        disarchive_top_dir="hello-1.0",
        disarchive_spec="(disarchive (version 0))",
    )
    expr = swh_fod_expression(result)
    assert isinstance(expr, SWHFodExpression)
    assert "outputHashMode = \"flat\"" in expr.nix_code
    assert "pkgs.stdenv.mkDerivation" in expr.nix_code
    assert f"vault/flat/{stripped}/raw" in expr.nix_code
    assert "tmp/wrapped/$topDir" in expr.nix_code
    assert 'topDir = "hello-1.0"' in expr.nix_code
    assert "disarchive assemble" in expr.nix_code
    assert "curl -L -f -o tmp/bundle.tar.bz2" in expr.nix_code
    assert "tar -xjf tmp/bundle.tar.bz2" in expr.nix_code


def test_disarchive_spec_is_quoted_for_nix():
    swhid = "swh:1:dir:" + "d" * 40
    result = make_result(
        fod=make_fod(method="flat", hash_algo="sha256", hash_hex="a" * 64),
        method=SWHLookupMethod.KNOWN_AFTER_DISARCHIVE,
        swhid=swhid,
        disarchive_swhid=swhid,
        disarchive_spec='(name "foo\\"bar")',
    )
    expr = swh_fod_expression(result)
    # The spec is embedded as a Nix double-quoted string, so quotes and
    # backslashes must be escaped.
    assert r'(name \"foo\\\"bar\")' in expr.nix_code


def test_unknown_result_returns_none():
    result = make_result(known=False)
    assert swh_fod_expression(result) is None


def test_unsupported_method_returns_none():
    result = make_result(method=SWHLookupMethod.UNSUPPORTED)
    assert swh_fod_expression(result) is None


def test_swh_fods_expression():
    exprs = [
        SWHFodExpression(label="a", nix_code="builtins.fetchurl { url = \"u\"; name = \"a\"; }"),
        SWHFodExpression(label="b", nix_code="builtins.fetchurl { url = \"u\"; name = \"b\"; }"),
    ]
    code = swh_fods_expression(exprs, name="test-farm")
    assert code.startswith("{ pkgs ? import (builtins.fetchTarball")
    assert code.endswith("}\n")
    assert '"a" = builtins.fetchurl' in code
    assert '"b" = builtins.fetchurl' in code
    assert "github.com/NixOS/nixpkgs/archive/" in code


def test_write_swh_fods_nix(tmp_path):
    results = [
        make_result(
            fod=make_fod(drv_path="/nix/store/a.drv", name="a"),
            method=SWHLookupMethod.CONTENT_HASH,
            swhid="swh:1:cnt:" + "b" * 40,
        ),
        make_result(known=False),
    ]
    path = tmp_path / "out.nix"
    expressions = write_swh_fods_nix(str(path), results)
    assert len(expressions) == 1
    assert path.exists()
    text = path.read_text()
    assert text.startswith("{ pkgs ? import (builtins.fetchTarball")
    assert "builtin:fetchurl" in text


def test_write_swh_fods_nix_calls_on_log(tmp_path):
    results = [
        make_result(
            method=SWHLookupMethod.CONTENT_HASH,
            swhid="swh:1:cnt:" + "b" * 40,
        ),
    ]
    logs = []
    write_swh_fods_nix(str(tmp_path / "out.nix"), results, on_log=logs.append)
    assert any("wrote 1 SWH-backed FOD" in log for log in logs)


def test_vault_swhids_for_content_hash_returns_empty():
    result = make_result(
        method=SWHLookupMethod.CONTENT_HASH,
        swhid="swh:1:cnt:" + "b" * 40,
    )
    assert vault_swhids_for_results([result]) == set()


def test_vault_swhids_for_directory_swhid():
    swhid = "swh:1:dir:" + "b" * 40
    result = make_result(
        fod=make_fod(method="nar", hash_algo="sha256", hash_hex="a" * 64),
        method=SWHLookupMethod.SWHID_KNOWN,
        swhid=swhid,
    )
    assert vault_swhids_for_results([result]) == {swhid}


def test_vault_swhids_for_build_and_identify_directory():
    swhid = "swh:1:dir:" + "c" * 40
    result = make_result(
        fod=make_fod(method="nar", hash_algo="sha256", hash_hex="a" * 64),
        method=SWHLookupMethod.BUILD_AND_IDENTIFY,
        swhid=swhid,
    )
    assert vault_swhids_for_results([result]) == {swhid}


def test_vault_swhids_for_disarchive_wrapped_directory():
    stripped = "swh:1:dir:" + "d" * 40
    result = make_result(
        fod=make_fod(method="flat", hash_algo="sha256", hash_hex="a" * 64),
        method=SWHLookupMethod.KNOWN_AFTER_DISARCHIVE,
        swhid=stripped,
        disarchive_swhid="swh:1:dir:" + "e" * 40,
        disarchive_top_dir="hello-1.0",
        disarchive_spec="(disarchive (version 0))",
    )
    assert vault_swhids_for_results([result]) == {stripped}


def test_vault_swhids_for_unknown_result_returns_empty():
    result = make_result(known=False)
    assert vault_swhids_for_results([result]) == set()


def test_vault_swhids_for_unsupported_method_returns_empty():
    result = make_result(method=SWHLookupMethod.UNSUPPORTED)
    assert vault_swhids_for_results([result]) == set()
