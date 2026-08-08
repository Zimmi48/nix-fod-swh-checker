import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from nix_fod_swh_checker import disarchive as disarchive_module
from nix_fod_swh_checker.disarchive import (
    DisarchiveError,
    try_disarchive,
    unpack_archive,
)
from nix_fod_swh_checker.models import FixedOutputDerivation, SWHCheckResult, SWHLookupMethod
from nix_fod_swh_checker.nix import NixCommandError
from nix_fod_swh_checker.swh import ContentLookupResult


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


class FakeSWHClient:
    def __init__(self, known_swhids=None):
        self.known_swhids = known_swhids or {}
        self.known_calls = []

    def lookup_known_swhids(self, swhids):
        self.known_calls.append(list(swhids))
        return {swhid: self.known_swhids.get(swhid, False) for swhid in swhids}


def _make_tar_archive(tmp_path, entries, suffix=".tar.gz"):
    archive = tmp_path / f"archive{suffix}"
    mode = "w:gz" if suffix.endswith(".gz") else "w"
    with tarfile.open(archive, mode) as tf:
        for name, content in entries:
            data = content.encode() if isinstance(content, str) else content
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return archive


def _make_zip_archive(tmp_path, entries):
    archive = tmp_path / "archive.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for name, content in entries:
            zf.writestr(name, content)
    return archive


def test_unpack_archive_tar_with_single_top_level_dir(tmp_path):
    archive = _make_tar_archive(
        tmp_path,
        [("src/file.txt", "hello")],
    )
    out = unpack_archive(str(archive))
    out_path = Path(out)
    assert (out_path / "src" / "file.txt").read_text() == "hello"


def test_unpack_archive_zip(tmp_path):
    archive = _make_zip_archive(
        tmp_path,
        [("src/file.txt", "hello")],
    )
    out = unpack_archive(str(archive))
    out_path = Path(out)
    assert (out_path / "src" / "file.txt").read_text() == "hello"


def test_unpack_archive_rejects_plain_file(tmp_path):
    archive = tmp_path / "not-an-archive.txt"
    archive.write_text("just a file")
    with pytest.raises(DisarchiveError):
        unpack_archive(str(archive))


def test_try_disarchive_returns_none_when_realisation_fails(monkeypatch):
    def fail_realise(fod, *, nix_binary, on_log=None):
        raise NixCommandError("boom")

    monkeypatch.setattr(disarchive_module, "realise_fod", fail_realise)
    result = try_disarchive(make_fod(), FakeSWHClient())
    assert result is None


def test_try_disarchive_returns_none_when_not_a_file(monkeypatch, tmp_path):
    monkeypatch.setattr(
        disarchive_module, "realise_fod", lambda fod, *, nix_binary, on_log=None: str(tmp_path)
    )
    result = try_disarchive(make_fod(), FakeSWHClient())
    assert result is None


def test_try_disarchive_returns_none_when_unpack_fails(monkeypatch, tmp_path):
    archive = tmp_path / "archive.txt"
    archive.write_text("not an archive")
    monkeypatch.setattr(
        disarchive_module, "realise_fod", lambda fod, *, nix_binary, on_log=None: str(archive)
    )
    result = try_disarchive(make_fod(), FakeSWHClient())
    assert result is None


def test_try_disarchive_known_directory_swhid(monkeypatch, tmp_path):
    swhid = "swh:1:dir:" + "b" * 40
    archive = _make_tar_archive(tmp_path, [("src/file.txt", "hello")])

    monkeypatch.setattr(
        disarchive_module, "realise_fod", lambda fod, *, nix_binary, on_log=None: str(archive)
    )
    monkeypatch.setattr(
        disarchive_module,
        "compute_swhid",
        lambda path, *, swh_binary, on_log=None, timeout=None: swhid,
    )

    client = FakeSWHClient(known_swhids={swhid: True})
    result = try_disarchive(make_fod(), client)
    assert isinstance(result, SWHCheckResult)
    assert result.known is True
    assert result.method == SWHLookupMethod.KNOWN_AFTER_DISARCHIVE
    assert result.swhid == swhid
    assert result.swh_url == f"https://archive.softwareheritage.org/{swhid}"


def test_try_disarchive_unknown_directory_swhid(monkeypatch, tmp_path):
    swhid = "swh:1:dir:" + "c" * 40
    archive = _make_tar_archive(tmp_path, [("src/file.txt", "hello")])

    monkeypatch.setattr(
        disarchive_module, "realise_fod", lambda fod, *, nix_binary, on_log=None: str(archive)
    )
    monkeypatch.setattr(
        disarchive_module,
        "compute_swhid",
        lambda path, *, swh_binary, on_log=None, timeout=None: swhid,
    )

    client = FakeSWHClient()
    result = try_disarchive(make_fod(), client)
    assert isinstance(result, SWHCheckResult)
    assert result.known is False
    assert result.method == SWHLookupMethod.KNOWN_AFTER_DISARCHIVE
    assert result.swhid == swhid
    assert result.swh_url is None
