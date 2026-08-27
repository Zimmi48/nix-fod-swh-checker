from pathlib import Path

from nix_archive_src.checkpoint import (
    default_checkpoint_path,
    load_checkpoint,
    save_checkpoint,
)
from nix_archive_src.models import FixedOutputDerivation, SWHCheckResult, SWHLookupMethod


def make_result(**overrides):
    fod = FixedOutputDerivation(
        drv_path="/nix/store/x.drv",
        output_name="out",
        output_path="/nix/store/y",
        name="x",
        method="flat",
        hash_algo="sha256",
        hash_hex="a" * 64,
    )
    defaults = dict(
        fod=fod,
        known=True,
        method=SWHLookupMethod.CONTENT_HASH,
        detail="content lookup by sha256:aaaa",
        swhid="swh:1:cnt:" + "a" * 40,
        swh_url="https://archive.softwareheritage.org/swh:1:cnt:" + "a" * 40,
    )
    defaults.update(overrides)
    return SWHCheckResult(**defaults)


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "checkpoint.json"
    result = make_result()

    save_checkpoint(path, "nixpkgs#hello", {result.fod.label: result})
    loaded = load_checkpoint(path)

    assert list(loaded.keys()) == [result.fod.label]
    loaded_result = loaded[result.fod.label]
    assert loaded_result.fod == result.fod
    assert loaded_result.known is True
    assert loaded_result.method == SWHLookupMethod.CONTENT_HASH
    assert loaded_result.detail == result.detail
    assert loaded_result.swhid == result.swhid
    assert loaded_result.swh_url == result.swh_url


def test_load_checkpoint_missing_file_returns_empty(tmp_path):
    assert load_checkpoint(tmp_path / "does-not-exist.json") == {}


def test_load_checkpoint_corrupt_file_returns_empty(tmp_path):
    path = tmp_path / "checkpoint.json"
    path.write_text("not json")
    assert load_checkpoint(path) == {}


def test_load_checkpoint_skips_unparseable_entries(tmp_path):
    path = tmp_path / "checkpoint.json"
    path.write_text(
        '{"installable": "x", "results": {"bad": {"fod": {}, "known": true, '
        '"method": "content_hash", "detail": "d"}}}'
    )
    assert load_checkpoint(path) == {}


def test_save_checkpoint_creates_parent_dirs(tmp_path):
    path = tmp_path / "nested" / "dir" / "checkpoint.json"
    result = make_result()
    save_checkpoint(path, "nixpkgs#hello", {result.fod.label: result})
    assert path.exists()


def test_default_checkpoint_path_is_stable_and_namespaced(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    path1 = default_checkpoint_path("nixpkgs#hello")
    path2 = default_checkpoint_path("nixpkgs#hello")
    path3 = default_checkpoint_path("nixpkgs#other")

    assert path1 == path2
    assert path1 != path3
    assert path1.parent == Path(tmp_path) / "nix-archive-src"
