import json

import pytest

from nix_fod_swh_checker import cli
from nix_fod_swh_checker.cli import _print_report, _result_to_dict
from nix_fod_swh_checker.models import FixedOutputDerivation, SWHCheckResult, SWHLookupMethod


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
    monkeypatch.setattr(cli, "dry_run_nix_file", lambda *a, **k: None)
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
    monkeypatch.setattr(cli, "dry_run_nix_file", lambda *a, **k: None)
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

    monkeypatch.setattr(cli, "dry_run_nix_file", lambda *a, **k: None)
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
    monkeypatch.setattr(cli, "dry_run_nix_file", lambda *a, **k: None)
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


def test_build_swh_fods_fails_when_vault_not_cooked(monkeypatch, capsys, tmp_path):
    dir_swhid = "swh:1:dir:" + "b" * 40
    nix_file = tmp_path / "swh-backed-fods.nix"
    nix_file.write_text(
        f'{{ pkgs ? {{}} }}: {{ "a" = pkgs.runCommand "x" {{}} "curl https://archive.softwareheritage.org/api/1/vault/flat/{dir_swhid}/raw"; }}\n'
    )

    built = []
    monkeypatch.setattr(
        cli, "build_nix_file", lambda path, attrs=None, **kwargs: built.append((path, attrs, kwargs))
    )
    monkeypatch.setattr(cli, "dry_run_nix_file", lambda *a, **k: None)
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
            return type("Task", (), {"status": "new"})()

    monkeypatch.setattr(cli, "SWHClient", lambda **kwargs: FakeClient())

    exit_code = cli.main(["build-swh-fods", str(nix_file), "--quiet"])
    err = capsys.readouterr().err
    assert exit_code == 1
    assert "not cooked" in err
    assert "cook-swh-fods" in err
    assert built == []


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

    def fake_write_swh_fods_nix(path, results):
        written.append((path, results))
        return []

    monkeypatch.setattr(cli, "write_swh_fods_nix", fake_write_swh_fods_nix)

    output = tmp_path / "swh-backed-fods.nix"
    exit_code = cli.main(
        ["generate-swh-fods", "nixpkgs#hello", "-i", str(results_json), "-o", str(output)]
    )

    assert exit_code == 0
    assert len(written) == 1
    assert written[0][0] == str(output)
    reloaded = written[0][1]
    assert len(reloaded) == 1
    assert reloaded[0].fod == result.fod


def test_build_swh_fods_passes_no_substitute(monkeypatch, capsys, tmp_path):
    nix_file = tmp_path / "swh-backed-fods.nix"
    nix_file.write_text('{ pkgs ? {} }: { "a" = builtins.fetchurl { url = "u"; }; }\n')

    built = []
    dry_run_calls = []

    def fake_build_nix_file(path, attrs=None, **kwargs):
        built.append((path, attrs, kwargs))

    def fake_dry_run_nix_file(path, attrs, **kwargs):
        dry_run_calls.append((path, attrs, kwargs))
        return []

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
