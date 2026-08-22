import json

import pytest

from nix_fod_swh_checker import cli
from nix_fod_swh_checker.cli import _print_report, _result_to_dict
from nix_fod_swh_checker.models import FixedOutputDerivation, SWHCheckResult, SWHLookupMethod
from nix_fod_swh_checker.nix import DryRunPlan


class _NullContextClient:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _fod(suffix):
    return FixedOutputDerivation(
        drv_path=f"/nix/store/{suffix}.drv",
        output_name="out",
        output_path=f"/nix/store/out-{suffix}",
        name=suffix,
        method="flat",
        hash_algo="sha256",
        hash_hex="a" * 64,
    )


def test_main_returns_1_on_nix_command_error(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise cli.NixCommandError("boom")

    monkeypatch.setattr(cli, "show_derivations_recursive", fail)
    exit_code = cli.main(["check", "nixpkgs#hello", "--quiet"])
    assert exit_code == 1
    assert "boom" in capsys.readouterr().err


def test_main_handles_keyboard_interrupt_before_any_fod_checked(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "show_derivations_recursive", fail)
    exit_code = cli.main(["check", "nixpkgs#hello", "--quiet", "--no-checkpoint"])
    err = capsys.readouterr().err

    assert exit_code == 130
    assert "interrupted" in err
    assert "Traceback" not in err
    assert "checked" not in err


def test_main_handles_keyboard_interrupt_mid_loop_and_saves_checkpoint(
    monkeypatch, capsys, tmp_path
):
    fod1, fod2 = _fod("a"), _fod("b")
    monkeypatch.setattr(cli, "show_derivations_recursive", lambda *a, **k: {})
    monkeypatch.setattr(
        cli, "iter_fixed_output_derivations", lambda derivations: iter([fod1, fod2])
    )
    monkeypatch.setattr(cli, "SWHClient", lambda **kwargs: _NullContextClient())

    calls = {"n": 0}

    def fake_check_fod(fod, client, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return SWHCheckResult(
                fod=fod, known=True, method=SWHLookupMethod.CONTENT_HASH, detail="d"
            )
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "check_fod", fake_check_fod)

    checkpoint_file = tmp_path / "ckpt.json"
    exit_code = cli.main(
        ["check", "nixpkgs#hello", "--quiet", "--checkpoint-file", str(checkpoint_file)]
    )
    err = capsys.readouterr().err

    assert exit_code == 130
    assert "Traceback" not in err
    assert "1 FOD(s)" in err
    assert str(checkpoint_file) in err

    saved = json.loads(checkpoint_file.read_text())
    assert list(saved["results"].keys()) == [fod1.label]


def test_check_warns_when_no_swh_api_token(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise cli.NixCommandError("boom")

    monkeypatch.setattr(cli, "show_derivations_recursive", fail)
    monkeypatch.delenv("SWH_API_TOKEN", raising=False)
    exit_code = cli.main(["check", "nixpkgs#hello"])
    err = capsys.readouterr().err

    assert exit_code == 1
    assert "warning: no Software Heritage API token" in err


def test_check_does_not_warn_when_token_is_set(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise cli.NixCommandError("boom")

    monkeypatch.setattr(cli, "show_derivations_recursive", fail)
    monkeypatch.setenv("SWH_API_TOKEN", "secret-token")
    exit_code = cli.main(["check", "nixpkgs#hello"])
    err = capsys.readouterr().err

    assert exit_code == 1
    assert "warning: no Software Heritage API token" not in err


def test_main_without_subcommand_exits_cleanly(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main([])

    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "required: command" in err


def test_check_writes_json_output_with_all_documented_fields(monkeypatch, tmp_path):
    fod = _fod("a")
    result = SWHCheckResult(
        fod=fod,
        known=True,
        method=SWHLookupMethod.KNOWN_AFTER_DISARCHIVE,
        detail="unpacked archive",
        swhid="swh:1:dir:" + "b" * 40,
        swh_url="https://archive.softwareheritage.org/swh:1:dir:" + "b" * 40,
        disarchive_spec="(disarchive (version 0))",
        disarchive_swhid="swh:1:dir:" + "c" * 40,
        disarchive_top_dir="hello-1.0",
    )

    monkeypatch.setattr(cli, "show_derivations_recursive", lambda *a, **k: {})
    monkeypatch.setattr(cli, "iter_fixed_output_derivations", lambda d: iter([fod]))
    monkeypatch.setattr(cli, "SWHClient", lambda **kwargs: _NullContextClient())
    monkeypatch.setattr(cli, "check_fod", lambda *a, **k: result)
    monkeypatch.setattr(cli, "default_checkpoint_path", lambda installable: tmp_path / "ckpt.json")

    output = tmp_path / "results.json"
    exit_code = cli.main(
        ["check", "nixpkgs#hello", "--quiet", "--no-checkpoint", "-o", str(output)]
    )
    assert exit_code == 0

    payload = json.loads(output.read_text())
    assert isinstance(payload, list) and len(payload) == 1
    entry = payload[0]

    # Fields documented in the specification for JSON output.
    assert set(entry.keys()) == {
        "fod",
        "known",
        "method",
        "detail",
        "swhid",
        "swh_url",
        "disarchive_spec",
        "disarchive_swhid",
        "disarchive_top_dir",
        "origin_urls",
    }
    assert entry["known"] is True
    assert entry["method"] == "known_after_disarchive"
    assert entry["detail"] == "unpacked archive"
    assert entry["swhid"] == "swh:1:dir:" + "b" * 40
    assert entry["swh_url"].startswith("https://archive.softwareheritage.org/")
    assert entry["disarchive_spec"] == "(disarchive (version 0))"
    assert entry["disarchive_swhid"] == "swh:1:dir:" + "c" * 40
    assert entry["disarchive_top_dir"] == "hello-1.0"
    assert entry["origin_urls"] == []

    fod_obj = entry["fod"]
    assert set(fod_obj.keys()) == {
        "drv_path",
        "output_name",
        "output_path",
        "name",
        "method",
        "hash_algo",
        "hash_hex",
        "label",
        "origin_urls",
        "executable",
    }
    assert fod_obj["drv_path"] == fod.drv_path
    assert fod_obj["output_name"] == "out"
    assert fod_obj["label"] == fod.label
    assert fod_obj["origin_urls"] == []


def test_check_output_prints_report_unless_quiet(monkeypatch, capsys, tmp_path):
    fod = _fod("a")
    result = SWHCheckResult(
        fod=fod, known=True, method=SWHLookupMethod.CONTENT_HASH, detail="known"
    )

    monkeypatch.setattr(cli, "show_derivations_recursive", lambda *a, **k: {})
    monkeypatch.setattr(cli, "iter_fixed_output_derivations", lambda d: iter([fod]))
    monkeypatch.setattr(cli, "SWHClient", lambda **kwargs: _NullContextClient())
    monkeypatch.setattr(cli, "check_fod", lambda *a, **k: result)
    monkeypatch.setattr(cli, "default_checkpoint_path", lambda installable: tmp_path / "ckpt.json")

    output = tmp_path / "results.json"
    exit_code = cli.main(
        ["check", "nixpkgs#hello", "--no-checkpoint", "-o", str(output)]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "[KNOWN]" in out
    assert "1 FOD(s) checked: 1 known, 0 known after disarchive, 0 unknown, 0 undetermined" in out
    assert json.loads(output.read_text())


def test_check_output_with_quiet_does_not_print_report(monkeypatch, capsys, tmp_path):
    fod = _fod("a")
    result = SWHCheckResult(
        fod=fod, known=True, method=SWHLookupMethod.CONTENT_HASH, detail="known"
    )

    monkeypatch.setattr(cli, "show_derivations_recursive", lambda *a, **k: {})
    monkeypatch.setattr(cli, "iter_fixed_output_derivations", lambda d: iter([fod]))
    monkeypatch.setattr(cli, "SWHClient", lambda **kwargs: _NullContextClient())
    monkeypatch.setattr(cli, "check_fod", lambda *a, **k: result)
    monkeypatch.setattr(cli, "default_checkpoint_path", lambda installable: tmp_path / "ckpt.json")

    output = tmp_path / "results.json"
    exit_code = cli.main(
        ["check", "nixpkgs#hello", "--quiet", "--no-checkpoint", "-o", str(output)]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "[KNOWN]" not in out
    assert "FOD(s) checked" not in out
    assert json.loads(output.read_text())


def test_check_only_unknown_filters_report(monkeypatch, capsys, tmp_path):
    fod_known = _fod("known")
    fod_unknown = _fod("unknown")

    def fake_check_fod(fod, *args, **kwargs):
        if fod.name == "known":
            return SWHCheckResult(
                fod=fod, known=True, method=SWHLookupMethod.CONTENT_HASH, detail="known"
            )
        return SWHCheckResult(
            fod=fod, known=False, method=SWHLookupMethod.CONTENT_HASH, detail="not known"
        )

    monkeypatch.setattr(cli, "show_derivations_recursive", lambda *a, **k: {})
    monkeypatch.setattr(
        cli, "iter_fixed_output_derivations", lambda d: iter([fod_known, fod_unknown])
    )
    monkeypatch.setattr(cli, "SWHClient", lambda **kwargs: _NullContextClient())
    monkeypatch.setattr(cli, "check_fod", fake_check_fod)
    monkeypatch.setattr(cli, "default_checkpoint_path", lambda installable: tmp_path / "ckpt.json")

    exit_code = cli.main(
        ["check", "nixpkgs#hello", "--no-checkpoint", "--only-unknown"]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    # The unknown FOD's label contains "/nix/store/unknown.drv", so "KNOWN"
    # must not appear as a status label.
    assert "[KNOWN]" not in out
    assert "[UNKNOWN]" in out
    # The summary still counts all FODs that were checked, even though only
    # unknown ones are printed.
    assert "1 FOD(s) checked: 0 known, 0 known after disarchive, 1 unknown, 0 undetermined" in out


def test_check_no_fods_found_returns_zero(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "show_derivations_recursive", lambda *a, **k: {})
    monkeypatch.setattr(cli, "iter_fixed_output_derivations", lambda d: iter([]))

    exit_code = cli.main(["check", "nixpkgs#hello", "--quiet", "--no-checkpoint"])
    err = capsys.readouterr().err
    assert exit_code == 0
    assert "no fixed-output derivations found" in err


def test_check_resumes_from_existing_checkpoint(monkeypatch, capsys, tmp_path):
    fod_a = _fod("a")
    fod_b = _fod("b")
    existing = SWHCheckResult(
        fod=fod_a, known=True, method=SWHLookupMethod.CONTENT_HASH, detail="known"
    )
    checkpoint = tmp_path / "ckpt.json"
    from nix_fod_swh_checker.checkpoint import save_checkpoint

    save_checkpoint(checkpoint, "nixpkgs#hello", {fod_a.label: existing})

    checked: list[str] = []

    def fake_check_fod(fod, *args, **kwargs):
        checked.append(fod.label)
        return SWHCheckResult(
            fod=fod, known=False, method=SWHLookupMethod.CONTENT_HASH, detail="not known"
        )

    monkeypatch.setattr(cli, "show_derivations_recursive", lambda *a, **k: {})
    monkeypatch.setattr(cli, "iter_fixed_output_derivations", lambda d: iter([fod_a, fod_b]))
    monkeypatch.setattr(cli, "SWHClient", lambda **kwargs: _NullContextClient())
    monkeypatch.setattr(cli, "check_fod", fake_check_fod)

    exit_code = cli.main(
        ["check", "nixpkgs#hello", "--checkpoint-file", str(checkpoint), "--quiet"]
    )
    assert exit_code == 0
    assert checked == [fod_b.label]


def test_check_retry_unknown_rechecks_unknown_fods(monkeypatch, capsys, tmp_path):
    fod_known = _fod("known")
    fod_unknown = _fod("unknown")
    fod_undetermined = _fod("undetermined")
    checkpoint = tmp_path / "ckpt.json"
    from nix_fod_swh_checker.checkpoint import save_checkpoint

    save_checkpoint(
        checkpoint,
        "nixpkgs#hello",
        {
            fod_known.label: SWHCheckResult(
                fod=fod_known, known=True, method=SWHLookupMethod.CONTENT_HASH, detail="known"
            ),
            fod_unknown.label: SWHCheckResult(
                fod=fod_unknown,
                known=False,
                method=SWHLookupMethod.CONTENT_HASH,
                detail="not known",
            ),
            fod_undetermined.label: SWHCheckResult(
                fod=fod_undetermined,
                known=None,
                method=SWHLookupMethod.UNDETERMINED,
                detail="timed out",
            ),
        },
    )

    checked: list[str] = []

    def fake_check_fod(fod, *args, **kwargs):
        checked.append(fod.label)
        return SWHCheckResult(
            fod=fod, known=True, method=SWHLookupMethod.CONTENT_HASH, detail="now known"
        )

    monkeypatch.setattr(cli, "show_derivations_recursive", lambda *a, **k: {})
    monkeypatch.setattr(
        cli,
        "iter_fixed_output_derivations",
        lambda d: iter([fod_known, fod_unknown, fod_undetermined]),
    )
    monkeypatch.setattr(cli, "SWHClient", lambda **kwargs: _NullContextClient())
    monkeypatch.setattr(cli, "check_fod", fake_check_fod)

    exit_code = cli.main(
        [
            "check",
            "nixpkgs#hello",
            "--checkpoint-file",
            str(checkpoint),
            "--quiet",
            "--retry-unknown",
        ]
    )
    assert exit_code == 0
    assert checked == [fod_unknown.label]

    saved = json.loads(checkpoint.read_text())
    assert saved["results"][fod_unknown.label]["known"] is True
    assert saved["results"][fod_unknown.label]["detail"] == "now known"
    assert saved["results"][fod_known.label]["known"] is True
    assert saved["results"][fod_undetermined.label]["known"] is None


def test_check_retry_undetermined_rechecks_undetermined_fods(monkeypatch, capsys, tmp_path):
    fod_known = _fod("known")
    fod_unknown = _fod("unknown")
    fod_undetermined = _fod("undetermined")
    checkpoint = tmp_path / "ckpt.json"
    from nix_fod_swh_checker.checkpoint import save_checkpoint

    save_checkpoint(
        checkpoint,
        "nixpkgs#hello",
        {
            fod_known.label: SWHCheckResult(
                fod=fod_known, known=True, method=SWHLookupMethod.CONTENT_HASH, detail="known"
            ),
            fod_unknown.label: SWHCheckResult(
                fod=fod_unknown,
                known=False,
                method=SWHLookupMethod.CONTENT_HASH,
                detail="not known",
            ),
            fod_undetermined.label: SWHCheckResult(
                fod=fod_undetermined,
                known=None,
                method=SWHLookupMethod.UNDETERMINED,
                detail="timed out",
            ),
        },
    )

    checked: list[str] = []

    def fake_check_fod(fod, *args, **kwargs):
        checked.append(fod.label)
        return SWHCheckResult(
            fod=fod, known=True, method=SWHLookupMethod.CONTENT_HASH, detail="now known"
        )

    monkeypatch.setattr(cli, "show_derivations_recursive", lambda *a, **k: {})
    monkeypatch.setattr(
        cli,
        "iter_fixed_output_derivations",
        lambda d: iter([fod_known, fod_unknown, fod_undetermined]),
    )
    monkeypatch.setattr(cli, "SWHClient", lambda **kwargs: _NullContextClient())
    monkeypatch.setattr(cli, "check_fod", fake_check_fod)

    exit_code = cli.main(
        [
            "check",
            "nixpkgs#hello",
            "--checkpoint-file",
            str(checkpoint),
            "--quiet",
            "--retry-undetermined",
        ]
    )
    assert exit_code == 0
    assert checked == [fod_undetermined.label]

    saved = json.loads(checkpoint.read_text())
    assert saved["results"][fod_undetermined.label]["known"] is True
    assert saved["results"][fod_undetermined.label]["detail"] == "now known"
    assert saved["results"][fod_known.label]["known"] is True
    assert saved["results"][fod_unknown.label]["known"] is False


def test_check_swh_error_in_loop_is_warning_not_fatal(monkeypatch, capsys, tmp_path):
    fod = _fod("a")

    def fake_check_fod(*args, **kwargs):
        raise cli.SWHError("API down")

    monkeypatch.setattr(cli, "show_derivations_recursive", lambda *a, **k: {})
    monkeypatch.setattr(cli, "iter_fixed_output_derivations", lambda d: iter([fod]))
    monkeypatch.setattr(cli, "SWHClient", lambda **kwargs: _NullContextClient())
    monkeypatch.setattr(cli, "check_fod", fake_check_fod)

    exit_code = cli.main(["check", "nixpkgs#hello", "--no-checkpoint"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "warning: API down" in captured.err
    assert "nothing to report" in captured.out


def test_print_report_shows_known_after_disarchive_separately(capsys):
    fod = _fod("a")
    results = [
        SWHCheckResult(fod=fod, known=True, method=SWHLookupMethod.CONTENT_HASH, detail="d"),
        SWHCheckResult(
            fod=fod,
            known=True,
            method=SWHLookupMethod.KNOWN_AFTER_DISARCHIVE,
            detail="unpacked archive",
            swhid="swh:1:dir:" + "b" * 40,
        ),
        SWHCheckResult(fod=fod, known=False, method=SWHLookupMethod.SWHID_KNOWN, detail="d"),
    ]
    _print_report(results)
    out = capsys.readouterr().out
    assert "KNOWN AFTER DISARCHIVE" in out
    assert "1 known, 1 known after disarchive, 1 unknown, 0 undetermined" in out


def test_cook_swh_fods_without_checkpoint_returns_error(capsys, tmp_path):
    checkpoint = tmp_path / "missing.json"
    exit_code = cli.main(
        ["cook-swh-fods", "nixpkgs#hello", "--checkpoint-file", str(checkpoint)]
    )
    err = capsys.readouterr().err
    assert exit_code == 1
    assert "no checkpoint found" in err


def test_cook_swh_fods_with_no_vault_needs_returns_early(capsys, tmp_path):
    result = SWHCheckResult(
        fod=_fod("a"),
        known=True,
        method=SWHLookupMethod.CONTENT_HASH,
        detail="known",
        swhid="swh:1:cnt:" + "b" * 40,
    )
    checkpoint = tmp_path / "ckpt.json"
    from nix_fod_swh_checker.checkpoint import save_checkpoint

    save_checkpoint(checkpoint, "nixpkgs#hello", {result.fod.label: result})

    exit_code = cli.main(
        ["cook-swh-fods", "nixpkgs#hello", "--checkpoint-file", str(checkpoint), "--quiet"]
    )
    err = capsys.readouterr().err
    assert exit_code == 0
    assert "no vault flat archives need cooking" in err


def test_cook_swh_fods_requests_cooking_and_exits(monkeypatch, capsys, tmp_path):
    dir_swhid = "swh:1:dir:" + "b" * 40
    result = SWHCheckResult(
        fod=_fod("a"),
        known=True,
        method=SWHLookupMethod.SWHID_KNOWN,
        detail="known",
        swhid=dir_swhid,
    )
    checkpoint = tmp_path / "ckpt.json"
    from nix_fod_swh_checker.checkpoint import save_checkpoint

    save_checkpoint(checkpoint, "nixpkgs#hello", {result.fod.label: result})

    cooked = []

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def ensure_vault_flat_cooking(self, swhid):
            cooked.append(swhid)
            return type("Task", (), {"status": "new"})()

    monkeypatch.setattr(cli, "SWHClient", lambda **kwargs: FakeClient())

    exit_code = cli.main(
        ["cook-swh-fods", "nixpkgs#hello", "--checkpoint-file", str(checkpoint)]
    )
    err = capsys.readouterr().err
    assert exit_code == 0
    assert cooked == [dir_swhid]
    assert "cooking requests submitted" in err


def test_cook_swh_fods_from_nix_file(monkeypatch, capsys, tmp_path):
    nix_file = tmp_path / "swh-backed-fods.nix"
    swhid = "swh:1:dir:" + "b" * 40
    nix_file.write_text(
        f'{{ pkgs ? {{}} }}: {{\n  "a" = builtins.fetchTarball {{\n    url = "https://archive.softwareheritage.org/api/1/vault/flat/{swhid}/raw";\n  }};\n}}\n'
    )

    cooked = []

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def ensure_vault_flat_cooking(self, swhid):
            cooked.append(swhid)
            return type("Task", (), {"status": "new"})()

    monkeypatch.setattr(cli, "SWHClient", lambda **kwargs: FakeClient())

    exit_code = cli.main(["cook-swh-fods", str(nix_file), "--quiet"])
    err = capsys.readouterr().err
    assert exit_code == 0
    assert cooked == [swhid]


def test_cook_swh_fods_reports_cooking_error(monkeypatch, capsys, tmp_path):
    dir_swhid = "swh:1:dir:" + "b" * 40
    result = SWHCheckResult(
        fod=_fod("a"),
        known=True,
        method=SWHLookupMethod.SWHID_KNOWN,
        detail="known",
        swhid=dir_swhid,
    )
    checkpoint = tmp_path / "ckpt.json"
    from nix_fod_swh_checker.checkpoint import save_checkpoint

    save_checkpoint(checkpoint, "nixpkgs#hello", {result.fod.label: result})

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def ensure_vault_flat_cooking(self, swhid, **kwargs):
            raise cli.SWHError("cooking failed")

    monkeypatch.setattr(cli, "SWHClient", lambda **kwargs: FakeClient())

    exit_code = cli.main(
        ["cook-swh-fods", "nixpkgs#hello", "--checkpoint-file", str(checkpoint), "--quiet"]
    )
    err = capsys.readouterr().err
    assert exit_code == 1
    assert "cooking failed" in err


def test_build_swh_fods_without_checkpoint_returns_error(capsys, tmp_path):
    checkpoint = tmp_path / "missing.json"
    exit_code = cli.main(
        ["build-swh-fods", "nixpkgs#hello", "--checkpoint-file", str(checkpoint)]
    )
    err = capsys.readouterr().err
    assert exit_code == 1
    assert "no checkpoint found" in err


def test_build_swh_fods_with_no_known_fods_returns_early(capsys, tmp_path):
    result = SWHCheckResult(
        fod=_fod("a"),
        known=False,
        method=SWHLookupMethod.CONTENT_HASH,
        detail="not known",
    )
    checkpoint = tmp_path / "ckpt.json"
    from nix_fod_swh_checker.checkpoint import save_checkpoint

    save_checkpoint(checkpoint, "nixpkgs#hello", {result.fod.label: result})

    output = tmp_path / "out.nix"
    exit_code = cli.main(
        [
            "build-swh-fods",
            "nixpkgs#hello",
            "--checkpoint-file",
            str(checkpoint),
            "-o",
            str(output),
            "--quiet",
        ]
    )
    err = capsys.readouterr().err
    assert exit_code == 0
    assert "no SWH-backed FODs to build" in err
    assert output.exists()


def test_build_swh_fods_builds_generated_expression(monkeypatch, capsys, tmp_path):
    dir_swhid = "swh:1:dir:" + "b" * 40
    result = SWHCheckResult(
        fod=_fod("a"),
        known=True,
        method=SWHLookupMethod.SWHID_KNOWN,
        detail="known",
        swhid=dir_swhid,
    )
    checkpoint = tmp_path / "ckpt.json"
    from nix_fod_swh_checker.checkpoint import save_checkpoint

    save_checkpoint(checkpoint, "nixpkgs#hello", {result.fod.label: result})

    output = tmp_path / "out.nix"

    built = []
    monkeypatch.setattr(
        cli, "build_nix_file", lambda path, attrs=None, **kwargs: built.append((path, attrs, kwargs))
    )
    monkeypatch.setattr(
        cli,
        "dry_run_nix_file",
        lambda *a, **k: DryRunPlan(
            plan=[{"outputs": {"out": "/nix/store/out-a"}, "drvPath": "/nix/store/a.drv"}],
            will_build={"/nix/store/a.drv"},
            will_fetch=set(),
        ),
    )
    monkeypatch.setattr(cli, "_list_attrs_in_nix_file", lambda path: ["a"])
    monkeypatch.setattr(
        cli, "_eval_nix_file_outputs", lambda path: {"a": "/nix/store/out-a"}
    )
    monkeypatch.setattr(cli, "_extract_vault_swhids_by_attr", lambda path: {"a": dir_swhid})
    monkeypatch.setattr(cli.os.path, "exists", lambda path: path == output)

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def get_vault_flat_task(self, swhid):
            return type("Task", (), {"status": "done"})()

    monkeypatch.setattr(cli, "SWHClient", lambda **kwargs: FakeClient())

    exit_code = cli.main(
        [
            "build-swh-fods",
            "nixpkgs#hello",
            "--checkpoint-file",
            str(checkpoint),
            "-o",
            str(output),
            "--quiet",
        ]
    )
    err = capsys.readouterr().err
    assert exit_code == 0
    assert built == [(str(output), ["a"], {"extra_args": [], "on_log": None})]
    assert output.exists()
    assert "built SWH-backed FOD(s)" in err


def test_build_swh_fods_from_nix_file(monkeypatch, capsys, tmp_path):
    nix_file = tmp_path / "swh-backed-fods.nix"
    nix_file.write_text('{ pkgs ? {} }: { "a" = builtins.fetchurl { url = "u"; }; }\n')

    built = []
    monkeypatch.setattr(
        cli, "build_nix_file", lambda path, attrs=None, **kwargs: built.append((path, attrs, kwargs))
    )
    monkeypatch.setattr(
        cli,
        "dry_run_nix_file",
        lambda *a, **k: DryRunPlan(plan=[], will_build=set(), will_fetch=set()),
    )
    monkeypatch.setattr(cli, "_list_attrs_in_nix_file", lambda path: ["a"])
    monkeypatch.setattr(
        cli, "_eval_nix_file_outputs", lambda path: {"a": "/nix/store/out-a"}
    )
    monkeypatch.setattr(cli, "_extract_vault_swhids_by_attr", lambda path: {})
    monkeypatch.setattr(cli.os.path, "exists", lambda path: False)

    exit_code = cli.main(["build-swh-fods", str(nix_file), "--quiet"])
    err = capsys.readouterr().err
    assert exit_code == 0
    assert built == [(str(nix_file), ["a"], {"extra_args": [], "on_log": None})]
    assert "built SWH-backed FOD(s)" in err


def test_request_archiving_without_input_returns_error(capsys):
    exit_code = cli.main(["request-archiving"])
    err = capsys.readouterr().err
    assert exit_code == 2
    assert "installable or -i/--json-input is required" in err


def test_request_archiving_without_checkpoint_returns_error(capsys, tmp_path):
    checkpoint = tmp_path / "missing.json"
    exit_code = cli.main(
        ["request-archiving", "nixpkgs#hello", "--checkpoint-file", str(checkpoint)]
    )
    err = capsys.readouterr().err
    assert exit_code == 1
    assert "no checkpoint found" in err


def test_request_archiving_dry_run_lists_unknown_origins(capsys, tmp_path):
    fod_tarball = _fod("tarball")
    fod_tarball.origin_urls = ["https://example.com/archive.tar.gz"]
    fod_git = FixedOutputDerivation(
        drv_path="/nix/store/git.drv",
        output_name="out",
        output_path="/nix/store/git-out",
        name="git",
        method="git",
        hash_algo="sha1",
        hash_hex="a" * 40,
        origin_urls=["https://example.com/repo.git"],
    )
    fod_known = _fod("known")
    fod_known.origin_urls = ["https://example.com/known.tar.gz"]
    fod_undetermined = _fod("undetermined")
    fod_undetermined.origin_urls = ["https://example.com/undetermined.tar.gz"]

    checkpoint = tmp_path / "ckpt.json"
    from nix_fod_swh_checker.checkpoint import save_checkpoint

    save_checkpoint(
        checkpoint,
        "nixpkgs#hello",
        {
            fod_tarball.label: SWHCheckResult(
                fod=fod_tarball,
                known=False,
                method=SWHLookupMethod.CONTENT_HASH,
                detail="not known",
                origin_urls=fod_tarball.origin_urls,
            ),
            fod_git.label: SWHCheckResult(
                fod=fod_git,
                known=False,
                method=SWHLookupMethod.SWHID_KNOWN,
                detail="not known",
                origin_urls=fod_git.origin_urls,
            ),
            fod_known.label: SWHCheckResult(
                fod=fod_known,
                known=True,
                method=SWHLookupMethod.CONTENT_HASH,
                detail="known",
                origin_urls=fod_known.origin_urls,
            ),
            fod_undetermined.label: SWHCheckResult(
                fod=fod_undetermined,
                known=None,
                method=SWHLookupMethod.UNDETERMINED,
                detail="could not determine",
                origin_urls=fod_undetermined.origin_urls,
            ),
        },
    )

    exit_code = cli.main(
        [
            "request-archiving",
            "nixpkgs#hello",
            "--checkpoint-file",
            str(checkpoint),
            "--dry-run",
        ]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "https://example.com/archive.tar.gz (tarball)" in out
    assert "https://example.com/repo.git (git)" in out
    assert "https://example.com/known.tar.gz" not in out
    assert "https://example.com/undetermined.tar.gz" not in out


def test_request_archiving_submits_requests_for_unknown_origins(monkeypatch, capsys, tmp_path):
    fod = _fod("a")
    fod.origin_urls = ["https://example.com/archive.tar.gz"]
    checkpoint = tmp_path / "ckpt.json"
    from nix_fod_swh_checker.checkpoint import save_checkpoint

    save_checkpoint(
        checkpoint,
        "nixpkgs#hello",
        {
            fod.label: SWHCheckResult(
                fod=fod,
                known=False,
                method=SWHLookupMethod.CONTENT_HASH,
                detail="not known",
                origin_urls=fod.origin_urls,
            ),
        },
    )

    requested = []

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def request_origin_save(self, origin_url, *, visit_type="tarball"):
            requested.append((origin_url, visit_type))
            return type(
                "SaveRequest",
                (),
                {
                    "id": 123,
                    "origin_url": origin_url,
                    "visit_type": visit_type,
                    "save_request_status": "accepted",
                    "save_task_status": "pending",
                },
            )()

    monkeypatch.setattr(cli, "SWHClient", lambda **kwargs: FakeClient())
    monkeypatch.setattr(cli, "_first_live_url", lambda urls, **kwargs: urls[0])

    exit_code = cli.main(
        ["request-archiving", "nixpkgs#hello", "--checkpoint-file", str(checkpoint)]
    )
    err = capsys.readouterr().err
    assert exit_code == 0
    assert requested == [("https://example.com/archive.tar.gz", "tarball")]
    assert "archiving requests submitted" in err


def test_request_archiving_from_json_input(monkeypatch, capsys, tmp_path):
    fod = _fod("a")
    fod.origin_urls = ["https://example.com/repo.git"]
    json_file = tmp_path / "results.json"
    json_file.write_text(
        json.dumps(
            [
                {
                    "fod": {
                        "drv_path": fod.drv_path,
                        "output_name": fod.output_name,
                        "output_path": fod.output_path,
                        "name": fod.name,
                        "method": "git",
                        "hash_algo": fod.hash_algo,
                        "hash_hex": fod.hash_hex,
                        "label": fod.label,
                    },
                    "known": False,
                    "method": "swhid_known",
                    "detail": "not known",
                    "origin_urls": fod.origin_urls,
                }
            ]
        )
    )

    requested = []

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def request_origin_save(self, origin_url, *, visit_type="tarball"):
            requested.append((origin_url, visit_type))
            return type(
                "SaveRequest",
                (),
                {
                    "id": 456,
                    "origin_url": origin_url,
                    "visit_type": visit_type,
                    "save_request_status": "accepted",
                    "save_task_status": "pending",
                },
            )()

    monkeypatch.setattr(cli, "SWHClient", lambda **kwargs: FakeClient())
    monkeypatch.setattr(cli, "_first_live_url", lambda urls, **kwargs: urls[0])

    exit_code = cli.main(["request-archiving", "-i", str(json_file)])
    assert exit_code == 0
    assert requested == [("https://example.com/repo.git", "git")]


def test_request_archiving_warns_when_no_origin_urls(capsys, tmp_path):
    fod = _fod("a")
    fod.origin_urls = []
    checkpoint = tmp_path / "ckpt.json"
    from nix_fod_swh_checker.checkpoint import save_checkpoint

    save_checkpoint(
        checkpoint,
        "nixpkgs#hello",
        {
            fod.label: SWHCheckResult(
                fod=fod,
                known=False,
                method=SWHLookupMethod.CONTENT_HASH,
                detail="not known",
                origin_urls=[],
            ),
        },
    )

    exit_code = cli.main(
        ["request-archiving", "nixpkgs#hello", "--checkpoint-file", str(checkpoint), "--quiet"]
    )
    err = capsys.readouterr().err
    assert exit_code == 0
    assert "warning: skipping" in err
    assert "no origin URLs" in err


def test_request_archiving_skips_undetermined_results(monkeypatch, capsys, tmp_path):
    fod_unknown = _fod("unknown")
    fod_unknown.origin_urls = ["https://example.com/archive.tar.gz"]
    fod_undetermined = _fod("undetermined")
    fod_undetermined.origin_urls = ["https://example.com/undetermined.tar.gz"]
    checkpoint = tmp_path / "ckpt.json"
    from nix_fod_swh_checker.checkpoint import save_checkpoint

    save_checkpoint(
        checkpoint,
        "nixpkgs#hello",
        {
            fod_unknown.label: SWHCheckResult(
                fod=fod_unknown,
                known=False,
                method=SWHLookupMethod.CONTENT_HASH,
                detail="not known",
                origin_urls=fod_unknown.origin_urls,
            ),
            fod_undetermined.label: SWHCheckResult(
                fod=fod_undetermined,
                known=None,
                method=SWHLookupMethod.UNDETERMINED,
                detail="could not determine",
                origin_urls=fod_undetermined.origin_urls,
            ),
        },
    )

    requested = []

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def request_origin_save(self, origin_url, *, visit_type="tarball"):
            requested.append(origin_url)
            return type(
                "SaveRequest",
                (),
                {
                    "id": 1,
                    "origin_url": origin_url,
                    "visit_type": visit_type,
                    "save_request_status": "accepted",
                    "save_task_status": "pending",
                },
            )()

    monkeypatch.setattr(cli, "SWHClient", lambda **kwargs: FakeClient())
    monkeypatch.setattr(cli, "_first_live_url", lambda urls, **kwargs: urls[0])

    exit_code = cli.main(
        [
            "request-archiving",
            "nixpkgs#hello",
            "--checkpoint-file",
            str(checkpoint),
            "--quiet",
        ]
    )

    err = capsys.readouterr().err
    assert exit_code == 0
    assert requested == ["https://example.com/archive.tar.gz"]
    assert "https://example.com/undetermined.tar.gz" not in err


def test_request_archiving_warns_when_all_urls_unreachable(monkeypatch, capsys, tmp_path):
    fod = _fod("a")
    fod.origin_urls = ["https://example.com/dead.tar.gz"]
    checkpoint = tmp_path / "ckpt.json"
    from nix_fod_swh_checker.checkpoint import save_checkpoint

    save_checkpoint(
        checkpoint,
        "nixpkgs#hello",
        {
            fod.label: SWHCheckResult(
                fod=fod,
                known=False,
                method=SWHLookupMethod.CONTENT_HASH,
                detail="not known",
                origin_urls=fod.origin_urls,
            ),
        },
    )

    monkeypatch.setattr(cli, "_first_live_url", lambda urls, **kwargs: None)

    exit_code = cli.main(
        ["request-archiving", "nixpkgs#hello", "--checkpoint-file", str(checkpoint), "--quiet"]
    )
    err = capsys.readouterr().err
    assert exit_code == 0
    assert "warning: skipping" in err
    assert "no reachable origin URL" in err


def test_request_archiving_selects_first_live_url(monkeypatch, capsys, tmp_path):
    fod = _fod("a")
    fod.origin_urls = ["https://example.com/dead.tar.gz", "https://example.com/live.tar.gz"]
    checkpoint = tmp_path / "ckpt.json"
    from nix_fod_swh_checker.checkpoint import save_checkpoint

    save_checkpoint(
        checkpoint,
        "nixpkgs#hello",
        {
            fod.label: SWHCheckResult(
                fod=fod,
                known=False,
                method=SWHLookupMethod.CONTENT_HASH,
                detail="not known",
                origin_urls=fod.origin_urls,
            ),
        },
    )

    requested = []

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def request_origin_save(self, origin_url, *, visit_type="tarball"):
            requested.append(origin_url)
            return type(
                "SaveRequest",
                (),
                {
                    "id": 1,
                    "origin_url": origin_url,
                    "visit_type": visit_type,
                    "save_request_status": "accepted",
                    "save_task_status": "pending",
                },
            )()

    monkeypatch.setattr(cli, "SWHClient", lambda **kwargs: FakeClient())
    monkeypatch.setattr(
        cli, "_first_live_url", lambda urls, **kwargs: "https://example.com/live.tar.gz"
    )

    exit_code = cli.main(
        ["request-archiving", "nixpkgs#hello", "--checkpoint-file", str(checkpoint), "--quiet"]
    )
    assert exit_code == 0
    assert requested == ["https://example.com/live.tar.gz"]


def test_request_archiving_handles_keyboard_interrupt(monkeypatch, capsys, tmp_path):
    fod = _fod("a")
    fod.origin_urls = ["https://example.com/archive.tar.gz"]
    checkpoint = tmp_path / "ckpt.json"
    from nix_fod_swh_checker.checkpoint import save_checkpoint

    save_checkpoint(
        checkpoint,
        "nixpkgs#hello",
        {
            fod.label: SWHCheckResult(
                fod=fod,
                known=False,
                method=SWHLookupMethod.CONTENT_HASH,
                detail="not known",
                origin_urls=fod.origin_urls,
            ),
        },
    )

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def request_origin_save(self, origin_url, *, visit_type="tarball"):
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "SWHClient", lambda **kwargs: FakeClient())
    monkeypatch.setattr(cli, "_first_live_url", lambda urls, **kwargs: urls[0])

    exit_code = cli.main(
        ["request-archiving", "nixpkgs#hello", "--checkpoint-file", str(checkpoint)]
    )
    err = capsys.readouterr().err
    assert exit_code == 130
    assert "interrupted" in err
    assert "Traceback" not in err


def test_request_archiving_reports_api_error_per_origin(monkeypatch, capsys, tmp_path):
    fod = _fod("a")
    fod.origin_urls = ["https://example.com/archive.tar.gz"]
    checkpoint = tmp_path / "ckpt.json"
    from nix_fod_swh_checker.checkpoint import save_checkpoint

    save_checkpoint(
        checkpoint,
        "nixpkgs#hello",
        {
            fod.label: SWHCheckResult(
                fod=fod,
                known=False,
                method=SWHLookupMethod.CONTENT_HASH,
                detail="not known",
                origin_urls=fod.origin_urls,
            ),
        },
    )

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def request_origin_save(self, origin_url, *, visit_type="tarball"):
            raise cli.SWHError("blocked")

    monkeypatch.setattr(cli, "SWHClient", lambda **kwargs: FakeClient())
    monkeypatch.setattr(cli, "_first_live_url", lambda urls, **kwargs: urls[0])

    exit_code = cli.main(
        ["request-archiving", "nixpkgs#hello", "--checkpoint-file", str(checkpoint), "--quiet"]
    )
    err = capsys.readouterr().err
    assert exit_code == 0
    assert "warning: blocked" in err


def test_build_swh_fods_reports_nix_build_error(monkeypatch, capsys, tmp_path):
    dir_swhid = "swh:1:dir:" + "b" * 40
    result = SWHCheckResult(
        fod=_fod("a"),
        known=True,
        method=SWHLookupMethod.SWHID_KNOWN,
        detail="known",
        swhid=dir_swhid,
    )
    checkpoint = tmp_path / "ckpt.json"
    from nix_fod_swh_checker.checkpoint import save_checkpoint

    save_checkpoint(checkpoint, "nixpkgs#hello", {result.fod.label: result})

    monkeypatch.setattr(
        cli,
        "dry_run_nix_file",
        lambda *a, **k: DryRunPlan(plan=[], will_build=set(), will_fetch=set()),
    )
    monkeypatch.setattr(cli, "_list_attrs_in_nix_file", lambda path: ["a"])
    monkeypatch.setattr(
        cli, "_eval_nix_file_outputs", lambda path: {"a": "/nix/store/out-a"}
    )
    monkeypatch.setattr(cli, "_extract_vault_swhids_by_attr", lambda path: {})
    monkeypatch.setattr(cli.os.path, "exists", lambda path: False)
    monkeypatch.setattr(
        cli,
        "build_nix_file",
        lambda path, attrs=None, **kwargs: (_ for _ in ()).throw(cli.NixCommandError("nix failed")),
    )

    output = tmp_path / "out.nix"
    exit_code = cli.main(
        [
            "build-swh-fods",
            "nixpkgs#hello",
            "--checkpoint-file",
            str(checkpoint),
            "-o",
            str(output),
            "--quiet",
        ]
    )
    err = capsys.readouterr().err
    assert exit_code == 1
    assert "nix failed" in err


def test_build_swh_fods_skips_already_present_paths(monkeypatch, capsys, tmp_path):
    nix_file = tmp_path / "swh-backed-fods.nix"
    nix_file.write_text(
        '{ pkgs ? {} }: { "a" = builtins.fetchurl { url = "u"; }; "b" = builtins.fetchurl { url = "v"; }; }\n'
    )

    built = []
    monkeypatch.setattr(
        cli, "build_nix_file", lambda path, attrs=None, **kwargs: built.append((path, attrs, kwargs))
    )
    monkeypatch.setattr(
        cli,
        "dry_run_nix_file",
        lambda *a, **k: DryRunPlan(plan=[], will_build=set(), will_fetch=set()),
    )
    monkeypatch.setattr(cli, "_list_attrs_in_nix_file", lambda path: ["a", "b"])
    monkeypatch.setattr(
        cli, "_eval_nix_file_outputs", lambda path: {"a": "/nix/store/out-a", "b": "/nix/store/out-b"}
    )
    monkeypatch.setattr(cli, "_extract_vault_swhids_by_attr", lambda path: {})
    monkeypatch.setattr(cli.os.path, "exists", lambda path: path == "/nix/store/out-a")

    exit_code = cli.main(["build-swh-fods", str(nix_file), "--quiet"])
    err = capsys.readouterr().err
    assert exit_code == 0
    assert built == [(str(nix_file), ["b"], {"extra_args": [], "on_log": None})]
    assert "already in the Nix store" not in err


def test_check_skip_disarchive_passes_flag_to_check_fod(monkeypatch, capsys, tmp_path):
    fod = _fod("a")
    passed = {}

    def fake_check_fod(fod, client, **kwargs):
        passed["skip_disarchive"] = kwargs.get("skip_disarchive")
        return SWHCheckResult(
            fod=fod, known=True, method=SWHLookupMethod.CONTENT_HASH, detail="known"
        )

    monkeypatch.setattr(cli, "show_derivations_recursive", lambda *a, **k: {})
    monkeypatch.setattr(cli, "iter_fixed_output_derivations", lambda d: iter([fod]))
    monkeypatch.setattr(cli, "SWHClient", lambda **kwargs: _NullContextClient())
    monkeypatch.setattr(cli, "check_fod", fake_check_fod)
    monkeypatch.setattr(cli, "default_checkpoint_path", lambda installable: tmp_path / "ckpt.json")

    exit_code = cli.main(
        ["check", "nixpkgs#hello", "--no-checkpoint", "--skip-disarchive", "--quiet"]
    )
    assert exit_code == 0
    assert passed["skip_disarchive"] is True


def test_build_swh_fods_skips_uncooked_vault_with_warning(monkeypatch, capsys, tmp_path):
    dir_swhid = "swh:1:dir:" + "b" * 40
    nix_file = tmp_path / "swh-backed-fods.nix"
    nix_file.write_text(
        f'{{ pkgs ? {{}} }}: {{ "a" = pkgs.runCommand "x" {{}} "curl https://archive.softwareheritage.org/api/1/vault/flat/{dir_swhid}/raw"; "b" = builtins.fetchurl {{ url = "u"; }} }}\n'
    )

    built = []
    monkeypatch.setattr(
        cli, "build_nix_file", lambda path, attrs=None, **kwargs: built.append((path, attrs, kwargs))
    )
    monkeypatch.setattr(
        cli,
        "dry_run_nix_file",
        lambda *a, **k: DryRunPlan(
            plan=[
                {"outputs": {"out": "/nix/store/out-a"}, "drvPath": "/nix/store/a.drv"},
                {"outputs": {"out": "/nix/store/out-b"}, "drvPath": "/nix/store/b.drv"},
            ],
            will_build={"/nix/store/a.drv", "/nix/store/b.drv"},
            will_fetch=set(),
        ),
    )
    monkeypatch.setattr(cli, "_list_attrs_in_nix_file", lambda path: ["a", "b"])
    monkeypatch.setattr(
        cli, "_eval_nix_file_outputs", lambda path: {"a": "/nix/store/out-a", "b": "/nix/store/out-b"}
    )
    monkeypatch.setattr(cli, "_extract_vault_swhids_by_attr", lambda path: {"a": dir_swhid})
    monkeypatch.setattr(cli.os.path, "exists", lambda path: False)

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def get_vault_flat_task(self, swhid):
            return type("Task", (), {"status": "new"})()

    monkeypatch.setattr(cli, "SWHClient", lambda **kwargs: FakeClient())

    exit_code = cli.main(["build-swh-fods", str(nix_file), "--quiet"])
    err = capsys.readouterr().err
    assert exit_code == 0
    assert "warning" in err
    assert "not cooked" in err
    assert "cook-swh-fods" in err
    assert built == [(str(nix_file), ["b"], {"extra_args": [], "on_log": None})]


def test_build_swh_fods_skips_vault_check_for_fetched_paths(monkeypatch, capsys, tmp_path):
    dir_swhid = "swh:1:dir:" + "b" * 40
    nix_file = tmp_path / "swh-backed-fods.nix"
    nix_file.write_text(
        f'{{ pkgs ? {{}} }}: {{ "a" = pkgs.runCommand "x" {{}} "curl https://archive.softwareheritage.org/api/1/vault/flat/{dir_swhid}/raw"; }}\n'
    )

    built = []
    vault_checked: list[str] = []
    monkeypatch.setattr(
        cli, "build_nix_file", lambda path, attrs=None, **kwargs: built.append((path, attrs, kwargs))
    )
    monkeypatch.setattr(
        cli,
        "dry_run_nix_file",
        lambda *a, **k: DryRunPlan(
            plan=[{"outputs": {"out": "/nix/store/out-a"}, "drvPath": "/nix/store/a.drv"}],
            will_build=set(),
            will_fetch={"/nix/store/out-a"},
        ),
    )
    monkeypatch.setattr(cli, "_list_attrs_in_nix_file", lambda path: ["a"])
    monkeypatch.setattr(
        cli, "_eval_nix_file_outputs", lambda path: {"a": "/nix/store/out-a"}
    )
    monkeypatch.setattr(cli, "_extract_vault_swhids_by_attr", lambda path: {"a": dir_swhid})
    monkeypatch.setattr(cli.os.path, "exists", lambda path: False)

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def get_vault_flat_task(self, swhid):
            vault_checked.append(swhid)
            return type("Task", (), {"status": "new"})()

    monkeypatch.setattr(cli, "SWHClient", lambda **kwargs: FakeClient())

    exit_code = cli.main(["build-swh-fods", str(nix_file), "--quiet"])
    err = capsys.readouterr().err
    assert exit_code == 0
    assert vault_checked == []
    assert built == [(str(nix_file), ["a"], {"extra_args": [], "on_log": None})]
    assert "built SWH-backed FOD(s)" in err


def test_generate_swh_fods_round_trips_json_input_with_label(monkeypatch, tmp_path):
    """``generate-swh-fods -i`` must tolerate the redundant ``label`` field emitted by ``check --json``."""
    result = SWHCheckResult(
        fod=_fod("a"),
        known=True,
        method=SWHLookupMethod.CONTENT_HASH,
        detail="known",
        swhid="swh:1:cnt:" + "b" * 40,
    )
    results_json = tmp_path / "results.json"
    results_json.write_text(json.dumps([_result_to_dict(result)]))

    written = []

    def fake_write_swh_fods_nix(path, results, *, on_log=None):
        written.append((path, results))
        return []

    monkeypatch.setattr(cli, "write_swh_fods_nix", fake_write_swh_fods_nix)

    output = tmp_path / "swh-backed-fods.nix"
    exit_code = cli.main(
        ["generate-swh-fods", "-i", str(results_json), "-o", str(output)]
    )

    assert exit_code == 0
    assert len(written) == 1
    assert written[0][0] == str(output)
    reloaded = written[0][1]
    assert len(reloaded) == 1
    assert reloaded[0].fod == result.fod


def test_generate_swh_fods_reads_default_checkpoint(monkeypatch, capsys, tmp_path):
    result = SWHCheckResult(
        fod=_fod("a"),
        known=True,
        method=SWHLookupMethod.CONTENT_HASH,
        detail="known",
        swhid="swh:1:cnt:" + "b" * 40,
    )
    checkpoint = tmp_path / "ckpt.json"
    from nix_fod_swh_checker.checkpoint import save_checkpoint

    save_checkpoint(checkpoint, "nixpkgs#hello", {result.fod.label: result})

    written = []
    monkeypatch.setattr(
        cli,
        "write_swh_fods_nix",
        lambda path, results, *, on_log=None: written.append((path, results)) or [],
    )
    monkeypatch.setattr(cli, "default_checkpoint_path", lambda installable: checkpoint)

    output = tmp_path / "out.nix"
    exit_code = cli.main(["generate-swh-fods", "nixpkgs#hello", "-o", str(output)])
    assert exit_code == 0
    assert len(written) == 1
    assert written[0][0] == str(output)
    assert len(written[0][1]) == 1


def test_generate_swh_fods_empty_checkpoint_returns_error(capsys, tmp_path):
    checkpoint = tmp_path / "ckpt.json"
    checkpoint.write_text('{"installable": "nixpkgs#hello", "results": {}}')

    exit_code = cli.main(
        ["generate-swh-fods", "nixpkgs#hello", "--checkpoint-file", str(checkpoint)]
    )
    err = capsys.readouterr().err
    assert exit_code == 1
    assert "no checkpoint found" in err


def test_generate_swh_fods_warns_and_skips_inexpressible_results(monkeypatch, capsys, tmp_path):
    """Known results that cannot be turned into expressions are skipped with a warning."""
    result = SWHCheckResult(
        fod=_fod("a"),
        known=True,
        method=SWHLookupMethod.SWHID_KNOWN,
        detail="known",
        swhid="swh:1:cnt:" + "b" * 40,
    )
    checkpoint = tmp_path / "ckpt.json"
    from nix_fod_swh_checker.checkpoint import save_checkpoint

    save_checkpoint(checkpoint, "nixpkgs#hello", {result.fod.label: result})

    import nix_fod_swh_checker.swh_fod as swh_fod_module

    monkeypatch.setattr(swh_fod_module, "swh_fod_expression", lambda r: None)

    exit_code = cli.main(
        ["generate-swh-fods", "nixpkgs#hello", "--checkpoint-file", str(checkpoint)]
    )
    err = capsys.readouterr().err
    assert exit_code == 0
    assert "warning" in err
    assert "cannot be turned into" in err
    assert "wrote 0 SWH-backed FOD expression(s)" in err


def test_generate_swh_fods_warns_via_write_swh_fods_nix(monkeypatch, capsys, tmp_path):
    """The warning for inexpressible results is emitted by write_swh_fods_nix."""
    result = SWHCheckResult(
        fod=_fod("a"),
        known=True,
        method=SWHLookupMethod.SWHID_KNOWN,
        detail="known",
        swhid="swh:1:cnt:" + "b" * 40,
    )
    checkpoint = tmp_path / "ckpt.json"
    from nix_fod_swh_checker.checkpoint import save_checkpoint

    save_checkpoint(checkpoint, "nixpkgs#hello", {result.fod.label: result})

    def fake_write_swh_fods_nix(path, results, *, on_log=None):
        if on_log:
            on_log(
                f"warning: {results[0].fod.label} is known to Software Heritage "
                "(method=swhid_known) but cannot be turned into a SWH-backed FOD expression"
            )
        return []

    monkeypatch.setattr(cli, "write_swh_fods_nix", fake_write_swh_fods_nix)

    exit_code = cli.main(
        ["generate-swh-fods", "nixpkgs#hello", "--checkpoint-file", str(checkpoint)]
    )
    err = capsys.readouterr().err
    assert exit_code == 0
    assert "warning" in err
    assert "cannot be turned into" in err


def test_build_swh_fods_passes_no_substitute(monkeypatch, capsys, tmp_path):
    nix_file = tmp_path / "swh-backed-fods.nix"
    nix_file.write_text('{ pkgs ? {} }: { "a" = builtins.fetchurl { url = "u"; }; }\n')

    built = []
    dry_run_calls = []

    def fake_build_nix_file(path, attrs=None, **kwargs):
        built.append((path, attrs, kwargs))

    def fake_dry_run_nix_file(path, attrs, **kwargs):
        dry_run_calls.append((path, attrs, kwargs))
        return DryRunPlan(plan=[], will_build=set(), will_fetch=set())

    monkeypatch.setattr(cli, "build_nix_file", fake_build_nix_file)
    monkeypatch.setattr(cli, "dry_run_nix_file", fake_dry_run_nix_file)
    monkeypatch.setattr(cli, "_list_attrs_in_nix_file", lambda path: ["a"])
    monkeypatch.setattr(
        cli, "_eval_nix_file_outputs", lambda path: {"a": "/nix/store/out-a"}
    )
    monkeypatch.setattr(cli, "_extract_vault_swhids_by_attr", lambda path: {})
    monkeypatch.setattr(cli.os.path, "exists", lambda path: False)

    exit_code = cli.main(["build-swh-fods", str(nix_file), "--quiet", "--no-substitute"])
    err = capsys.readouterr().err
    assert exit_code == 0
    assert built == [(str(nix_file), ["a"], {"extra_args": ["--no-substitute"], "on_log": None})]
    assert dry_run_calls[0][2]["no_substitute"] is True
    assert "--no-substitute" not in dry_run_calls[0][2]["extra_args"]


def test_build_swh_fods_passes_extra_nix_build_args(monkeypatch, capsys, tmp_path):
    nix_file = tmp_path / "swh-backed-fods.nix"
    nix_file.write_text('{ pkgs ? {} }: { "a" = builtins.fetchurl { url = "u"; }; }\n')

    built = []
    dry_run_calls = []

    def fake_build_nix_file(path, attrs=None, **kwargs):
        built.append((path, attrs, kwargs))

    def fake_dry_run_nix_file(path, attrs, **kwargs):
        dry_run_calls.append((path, attrs, kwargs))
        return DryRunPlan(plan=[], will_build=set(), will_fetch=set())

    monkeypatch.setattr(cli, "build_nix_file", fake_build_nix_file)
    monkeypatch.setattr(cli, "dry_run_nix_file", fake_dry_run_nix_file)
    monkeypatch.setattr(cli, "_list_attrs_in_nix_file", lambda path: ["a"])
    monkeypatch.setattr(
        cli, "_eval_nix_file_outputs", lambda path: {"a": "/nix/store/out-a"}
    )
    monkeypatch.setattr(cli, "_extract_vault_swhids_by_attr", lambda path: {})
    monkeypatch.setattr(cli.os.path, "exists", lambda path: False)

    exit_code = cli.main(
        [
            "build-swh-fods",
            str(nix_file),
            "--quiet",
            "--nix-build-arg",
            "--option sandbox false",
        ]
    )
    assert exit_code == 0
    assert dry_run_calls[0][2]["extra_args"] == ["--option sandbox false"]
    assert built == [
        (str(nix_file), ["a"], {"extra_args": ["--option sandbox false"], "on_log": None})
    ]


def test_build_swh_fods_reports_dry_run_failure(monkeypatch, capsys, tmp_path):
    nix_file = tmp_path / "swh-backed-fods.nix"
    nix_file.write_text('{ pkgs ? {} }: { "a" = builtins.fetchurl { url = "u"; }; }\n')

    def fake_dry_run_nix_file(*args, **kwargs):
        raise cli.NixCommandError("dry run failed")

    monkeypatch.setattr(cli, "dry_run_nix_file", fake_dry_run_nix_file)
    monkeypatch.setattr(cli, "_list_attrs_in_nix_file", lambda path: ["a"])
    monkeypatch.setattr(
        cli, "_eval_nix_file_outputs", lambda path: {"a": "/nix/store/out-a"}
    )

    exit_code = cli.main(["build-swh-fods", str(nix_file), "--quiet"])
    err = capsys.readouterr().err
    assert exit_code == 1
    assert "dry run failed" in err


def test_build_swh_fods_reports_swh_error_during_vault_check(monkeypatch, capsys, tmp_path):
    dir_swhid = "swh:1:dir:" + "b" * 40
    nix_file = tmp_path / "swh-backed-fods.nix"
    nix_file.write_text(
        f'{{ pkgs ? {{}} }}: {{ "a" = pkgs.runCommand "x" {{}} "curl https://archive.softwareheritage.org/api/1/vault/flat/{dir_swhid}/raw"; }}\n'
    )

    monkeypatch.setattr(
        cli,
        "dry_run_nix_file",
        lambda *a, **k: DryRunPlan(
            plan=[{"outputs": {"out": "/nix/store/out-a"}, "drvPath": "/nix/store/a.drv"}],
            will_build={"/nix/store/a.drv"},
            will_fetch=set(),
        ),
    )
    monkeypatch.setattr(cli, "_list_attrs_in_nix_file", lambda path: ["a"])
    monkeypatch.setattr(
        cli, "_eval_nix_file_outputs", lambda path: {"a": "/nix/store/out-a"}
    )
    monkeypatch.setattr(cli, "_extract_vault_swhids_by_attr", lambda path: {"a": dir_swhid})
    monkeypatch.setattr(cli.os.path, "exists", lambda path: False)

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def get_vault_flat_task(self, swhid):
            raise cli.SWHError("API down")

    monkeypatch.setattr(cli, "SWHClient", lambda **kwargs: FakeClient())

    exit_code = cli.main(["build-swh-fods", str(nix_file), "--quiet"])
    err = capsys.readouterr().err
    assert exit_code == 1
    assert "API down" in err


def test_build_swh_fods_all_outputs_already_present(monkeypatch, capsys, tmp_path):
    nix_file = tmp_path / "swh-backed-fods.nix"
    nix_file.write_text('{ pkgs ? {} }: { "a" = builtins.fetchurl { url = "u"; }; }\n')

    built = []
    monkeypatch.setattr(cli, "build_nix_file", lambda *a, **k: built.append(True))
    monkeypatch.setattr(
        cli,
        "dry_run_nix_file",
        lambda *a, **k: DryRunPlan(plan=[], will_build=set(), will_fetch=set()),
    )
    monkeypatch.setattr(cli, "_list_attrs_in_nix_file", lambda path: ["a"])
    monkeypatch.setattr(
        cli, "_eval_nix_file_outputs", lambda path: {"a": "/nix/store/out-a"}
    )
    monkeypatch.setattr(cli.os.path, "exists", lambda path: path == "/nix/store/out-a")

    exit_code = cli.main(["build-swh-fods", str(nix_file), "--quiet"])
    err = capsys.readouterr().err
    assert exit_code == 0
    assert "already in the Nix store" in err
    assert built == []


def test_check_no_checkpoint_and_checkpoint_file_are_incompatible(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["check", "nixpkgs#hello", "--no-checkpoint", "--checkpoint-file", "/tmp/x.json"])
    assert exc_info.value.code == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_check_retry_unknown_requires_checkpoint(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["check", "nixpkgs#hello", "--retry-unknown", "--no-checkpoint"])
    assert exc_info.value.code == 2
    assert "require a checkpoint" in capsys.readouterr().err


def test_check_retry_undetermined_requires_checkpoint(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["check", "nixpkgs#hello", "--retry-undetermined", "--no-checkpoint"])
    assert exc_info.value.code == 2
    assert "require a checkpoint" in capsys.readouterr().err


def test_check_retry_both_requires_checkpoint(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "check",
                "nixpkgs#hello",
                "--retry-unknown",
                "--retry-undetermined",
                "--no-checkpoint",
            ]
        )
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "require a checkpoint" in err
    assert "--retry-unknown" in err
    assert "--retry-undetermined" in err


def test_request_archiving_json_input_and_checkpoint_file_incompatible(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["request-archiving", "-i", "/tmp/x.json", "--checkpoint-file", "/tmp/y.json"])
    assert exc_info.value.code == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_request_archiving_installable_and_json_input_incompatible(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["request-archiving", "nixpkgs#hello", "-i", "/tmp/x.json"])
    assert exc_info.value.code == 2
    assert "cannot be combined with -i/--json-input" in capsys.readouterr().err


def test_generate_swh_fods_json_input_and_checkpoint_file_incompatible(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["generate-swh-fods", "nixpkgs#hello", "-i", "/tmp/x.json", "--checkpoint-file", "/tmp/y.json"])
    assert exc_info.value.code == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_generate_swh_fods_installable_and_json_input_incompatible(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["generate-swh-fods", "nixpkgs#hello", "-i", "/tmp/x.json"])
    assert exc_info.value.code == 2
    assert "cannot be combined with -i/--json-input" in capsys.readouterr().err


def test_cook_swh_fods_checkpoint_file_incompatible_with_nix_file(capsys, tmp_path):
    nix_file = tmp_path / "swh-backed-fods.nix"
    nix_file.write_text('{ pkgs ? {} }: {}\n')
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["cook-swh-fods", str(nix_file), "--checkpoint-file", str(tmp_path / "x.json")])
    assert exc_info.value.code == 2
    assert "cannot be used when <input> is a .nix file" in capsys.readouterr().err


def test_build_swh_fods_checkpoint_file_incompatible_with_nix_file(capsys, tmp_path):
    nix_file = tmp_path / "swh-backed-fods.nix"
    nix_file.write_text('{ pkgs ? {} }: {}\n')
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["build-swh-fods", str(nix_file), "--checkpoint-file", str(tmp_path / "x.json")])
    assert exc_info.value.code == 2
    assert "cannot be used when <input> is a .nix file" in capsys.readouterr().err


def test_build_swh_fods_output_incompatible_with_nix_file(capsys, tmp_path):
    nix_file = tmp_path / "swh-backed-fods.nix"
    nix_file.write_text('{ pkgs ? {} }: {}\n')
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["build-swh-fods", str(nix_file), "-o", str(tmp_path / "out.nix")])
    assert exc_info.value.code == 2
    assert "-o/--output cannot be used when <input> is a .nix file" in capsys.readouterr().err
