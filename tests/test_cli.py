import json

from nix_fod_swh_checker import cli
from nix_fod_swh_checker.cli import _print_report
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
    exit_code = cli.main(["nixpkgs#hello", "--quiet"])
    assert exit_code == 1
    assert "boom" in capsys.readouterr().err


def test_main_handles_keyboard_interrupt_before_any_fod_checked(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "show_derivations_recursive", fail)
    exit_code = cli.main(["nixpkgs#hello", "--quiet", "--no-checkpoint"])
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
        ["nixpkgs#hello", "--quiet", "--checkpoint-file", str(checkpoint_file)]
    )
    err = capsys.readouterr().err

    assert exit_code == 130
    assert "Traceback" not in err
    assert "1 FOD(s)" in err
    assert str(checkpoint_file) in err

    saved = json.loads(checkpoint_file.read_text())
    assert list(saved["results"].keys()) == [fod1.label]


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
