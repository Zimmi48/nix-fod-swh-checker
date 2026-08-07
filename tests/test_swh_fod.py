"""Tests for SWH-backed FOD expression generation."""
from __future__ import annotations

import pytest

from nix_fod_swh_checker.models import FixedOutputDerivation, SWHCheckResult, SWHLookupMethod
from nix_fod_swh_checker.swh_fod import (
    SWHFodExpression,
    link_farm_expression,
    swh_fod_expression,
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
    assert "outputHashMode = \"recursive\"" in expr.nix_code
    assert f"vault/flat/{swhid}/raw" in expr.nix_code
    assert "pkgs.curl" in expr.nix_code
    assert "pkgs.gnutar" in expr.nix_code


def test_build_and_identify_directory_expression():
    swhid = "swh:1:dir:" + "c" * 40
    result = make_result(
        fod=make_fod(method="nar", hash_algo="sha256", hash_hex="a" * 64),
        method=SWHLookupMethod.BUILD_AND_IDENTIFY,
        swhid=swhid,
    )
    expr = swh_fod_expression(result)
    assert isinstance(expr, SWHFodExpression)
    assert "outputHashMode = \"recursive\"" in expr.nix_code
    assert f"vault/flat/{swhid}/raw" in expr.nix_code


def test_disarchive_expression_requires_spec():
    swhid = "swh:1:dir:" + "d" * 40
    result = make_result(
        fod=make_fod(method="flat", hash_algo="sha256", hash_hex="a" * 64),
        method=SWHLookupMethod.KNOWN_AFTER_DISARCHIVE,
        swhid=swhid,
        disarchive_spec=None,
    )
    assert swh_fod_expression(result) is None


def test_disarchive_expression_with_spec():
    swhid = "swh:1:dir:" + "d" * 40
    result = make_result(
        fod=make_fod(method="flat", hash_algo="sha256", hash_hex="a" * 64),
        method=SWHLookupMethod.KNOWN_AFTER_DISARCHIVE,
        swhid=swhid,
        disarchive_spec="(disarchive (version 0))",
    )
    expr = swh_fod_expression(result)
    assert isinstance(expr, SWHFodExpression)
    assert "outputHashMode = \"flat\"" in expr.nix_code
    assert f"vault/flat/{swhid}/raw" in expr.nix_code
    assert "pkgs.disarchive" in expr.nix_code
    assert "disarchive assemble" in expr.nix_code
    assert "(disarchive (version 0))" in expr.nix_code


def test_disarchive_expression_escapes_eof_delimiter():
    swhid = "swh:1:dir:" + "d" * 40
    result = make_result(
        fod=make_fod(method="flat", hash_algo="sha256", hash_hex="a" * 64),
        method=SWHLookupMethod.KNOWN_AFTER_DISARCHIVE,
        swhid=swhid,
        disarchive_spec="DISARCHIVE_EOF",
    )
    expr = swh_fod_expression(result)
    # The line that would terminate the heredoc is indented so it is treated
    # as part of the specification rather than the closing delimiter.
    assert " DISARCHIVE_EOF" in expr.nix_code
    # The escaped spec line, the heredoc opening, and the closing delimiter.
    assert expr.nix_code.count("DISARCHIVE_EOF") == 3


def test_unknown_result_returns_none():
    result = make_result(known=False)
    assert swh_fod_expression(result) is None


def test_unsupported_method_returns_none():
    result = make_result(method=SWHLookupMethod.UNSUPPORTED)
    assert swh_fod_expression(result) is None


def test_link_farm_expression():
    exprs = [
        SWHFodExpression(label="a", nix_code="{ pkgs }: pkgs.hello"),
        SWHFodExpression(label="b", nix_code="{ pkgs }: pkgs.git"),
    ]
    code = link_farm_expression(exprs, name="test-farm")
    assert "pkgs.linkFarm test-farm" in code
    assert "{ name = a; path = ({ pkgs }: pkgs.hello) pkgs; }" in code
    assert "{ name = b; path = ({ pkgs }: pkgs.git) pkgs; }" in code


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
    assert "swh-backed-fods" in path.read_text()


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
