import io
import shutil
import tarfile

import pytest

from nix_fod_swh_checker import checker as checker_module
from nix_fod_swh_checker import disarchive as disarchive_module
from nix_fod_swh_checker.checker import check_fod
from nix_fod_swh_checker.models import FixedOutputDerivation, SWHCheckResult, SWHLookupMethod
from nix_fod_swh_checker.nix import NixCommandError
from nix_fod_swh_checker.swh import ContentLookupResult
from nix_fod_swh_checker.swhid import compute_swhid


# Directory SWHID for a tree containing a single file ``file.txt`` with the
# bytes ``b"hello"``, as computed by ``swh identify --no-filename``.
KNOWN_DIRECTORY_SWHID = "swh:1:dir:952dd0a0ff0d34ef3f52035c658e1d1ed56fd0c1"

DISARCHIVE_AVAILABLE = shutil.which("disarchive") is not None


class FakeSWHClient:
    def __init__(self, content_known=False, content_raw=None, known_swhids=None):
        self.content_known = content_known
        self.content_raw = content_raw
        self.known_swhids = known_swhids or {}
        self.content_calls = []

    def lookup_content(self, algo, hash_hex):
        self.content_calls.append((algo, hash_hex))
        return ContentLookupResult(known=self.content_known, raw=self.content_raw)

    def lookup_known_swhids(self, swhids):
        return {swhid: self.known_swhids.get(swhid, False) for swhid in swhids}


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


def test_check_fod_flat_known():
    fod = make_fod(method="flat", hash_algo="sha256", hash_hex="a" * 64)
    sha1_git = "b" * 40
    client = FakeSWHClient(content_known=True, content_raw={"checksums": {"sha1_git": sha1_git}})
    result = check_fod(fod, client)
    assert result.known is True
    assert result.method == SWHLookupMethod.CONTENT_HASH
    assert client.content_calls == [("sha256", "a" * 64)]
    assert result.swhid == f"swh:1:cnt:{sha1_git}"
    assert result.swh_url == f"https://archive.softwareheritage.org/swh:1:cnt:{sha1_git}"


def test_check_fod_flat_unknown(monkeypatch, tmp_path):
    fod = make_fod(method="flat", hash_algo="sha256", hash_hex="b" * 64)
    archive = _make_tar_archive(tmp_path, [("src/file.txt", "hello")])
    client = FakeSWHClient(content_known=False)

    monkeypatch.setattr(
        disarchive_module, "realise_fod", lambda fod, *, nix_binary, on_log=None: str(archive)
    )

    result = check_fod(fod, client)
    assert result.known is False
    assert result.method == SWHLookupMethod.KNOWN_AFTER_DISARCHIVE
    assert result.swhid == KNOWN_DIRECTORY_SWHID


def test_check_fod_git_method_known_as_content():
    swhid = "swh:1:cnt:" + "c" * 40
    fod = make_fod(method="git", hash_algo="sha1", hash_hex="c" * 40)
    client = FakeSWHClient(known_swhids={swhid: True})
    result = check_fod(fod, client)
    assert result.known is True
    assert result.method == SWHLookupMethod.SWHID_KNOWN
    assert swhid in result.detail
    assert result.swhid == swhid
    assert result.swh_url == f"https://archive.softwareheritage.org/{swhid}"


def test_check_fod_git_method_unknown(monkeypatch, tmp_path):
    fod = make_fod(method="git", hash_algo="sha1", hash_hex="d" * 40)
    archive = _make_tar_archive(tmp_path, [("src/file.txt", "hello")])
    client = FakeSWHClient()

    monkeypatch.setattr(
        disarchive_module, "realise_fod", lambda fod, *, nix_binary, on_log=None: str(archive)
    )

    result = check_fod(fod, client)
    assert result.known is False
    assert result.method == SWHLookupMethod.KNOWN_AFTER_DISARCHIVE
    assert result.swhid == KNOWN_DIRECTORY_SWHID


def test_check_fod_nar_method_builds_and_identifies(monkeypatch, tmp_path):
    fod = make_fod(method="nar", hash_algo="sha256", hash_hex="e" * 64)
    out_path = tmp_path / "out"
    out_path.mkdir()
    (out_path / "file.txt").write_text("hello")
    swhid = compute_swhid(str(out_path))

    monkeypatch.setattr(
        checker_module, "realise_fod", lambda fod, *, nix_binary, on_log=None: str(out_path)
    )

    client = FakeSWHClient(known_swhids={swhid: True})
    result = check_fod(fod, client)
    assert result.known is True
    assert result.method == SWHLookupMethod.BUILD_AND_IDENTIFY
    assert str(out_path) in result.detail
    assert swhid in result.detail
    assert result.swhid == swhid
    assert result.swh_url == f"https://archive.softwareheritage.org/{swhid}"


def test_check_fod_nar_method_unknown_swhid(monkeypatch, tmp_path):
    fod = make_fod(method="nar", hash_algo="sha256", hash_hex="1" * 64)
    out_path = tmp_path / "out"
    out_path.mkdir()
    (out_path / "file.txt").write_text("hello")
    swhid = compute_swhid(str(out_path))

    monkeypatch.setattr(
        checker_module, "realise_fod", lambda fod, *, nix_binary, on_log=None: str(out_path)
    )

    client = FakeSWHClient()
    result = check_fod(fod, client)
    assert result.known is False
    assert result.method == SWHLookupMethod.BUILD_AND_IDENTIFY
    assert result.swhid == swhid
    assert result.swh_url is None


def test_check_fod_nar_method_build_failure_is_undetermined(monkeypatch):
    fod = make_fod(method="nar", hash_algo="sha256", hash_hex="3" * 64)

    def fail_realise(fod, *, nix_binary, on_log=None):
        raise NixCommandError("boom")

    monkeypatch.setattr(checker_module, "realise_fod", fail_realise)

    client = FakeSWHClient()
    result = check_fod(fod, client)
    assert result.known is None
    assert result.method == SWHLookupMethod.UNDETERMINED
    assert "boom" in result.detail


def test_check_fod_nar_method_identify_failure_is_undetermined(monkeypatch, tmp_path):
    fod = make_fod(method="nar", hash_algo="sha256", hash_hex="4" * 64)
    out_path = tmp_path / "out"
    out_path.mkdir()
    (out_path / "file.txt").write_text("hello")

    monkeypatch.setattr(
        checker_module, "realise_fod", lambda fod, *, nix_binary, on_log=None: str(out_path)
    )

    result = check_fod(fod, client=FakeSWHClient(), swh_binary="does-not-exist")
    assert result.known is None
    assert result.method == SWHLookupMethod.UNDETERMINED
    assert "could not find" in result.detail


@pytest.mark.skipif(
    not DISARCHIVE_AVAILABLE,
    reason="disarchive binary is not available in this environment",
)
def test_check_fod_flat_unknown_but_known_after_disarchive(monkeypatch, tmp_path):
    fod = make_fod(method="flat", hash_algo="sha256", hash_hex="b" * 64)
    archive = _make_tar_archive(tmp_path, [("src/file.txt", "hello")])
    client = FakeSWHClient(content_known=False, known_swhids={KNOWN_DIRECTORY_SWHID: True})

    monkeypatch.setattr(
        disarchive_module, "realise_fod", lambda fod, *, nix_binary, on_log=None: str(archive)
    )

    result = check_fod(fod, client)
    assert result.known is True
    assert result.method == SWHLookupMethod.KNOWN_AFTER_DISARCHIVE
    assert result.swhid == KNOWN_DIRECTORY_SWHID
    assert result.disarchive_spec is not None


@pytest.mark.skipif(
    not DISARCHIVE_AVAILABLE,
    reason="disarchive binary is not available in this environment",
)
def test_check_fod_git_unknown_but_known_after_disarchive(monkeypatch, tmp_path):
    fod = make_fod(method="git", hash_algo="sha1", hash_hex="d" * 40)
    archive = _make_tar_archive(tmp_path, [("src/file.txt", "hello")])
    client = FakeSWHClient(known_swhids={KNOWN_DIRECTORY_SWHID: True})

    monkeypatch.setattr(
        disarchive_module, "realise_fod", lambda fod, *, nix_binary, on_log=None: str(archive)
    )

    result = check_fod(fod, client)
    assert result.known is True
    assert result.method == SWHLookupMethod.KNOWN_AFTER_DISARCHIVE
    assert result.swhid == KNOWN_DIRECTORY_SWHID
    assert result.disarchive_spec is not None
