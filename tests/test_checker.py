from nix_fod_swh_checker import checker as checker_module
from nix_fod_swh_checker.checker import check_fod
from nix_fod_swh_checker.models import FixedOutputDerivation, SWHCheckResult, SWHLookupMethod
from nix_fod_swh_checker.nix import NixCommandError
from nix_fod_swh_checker.swh import ContentLookupResult
from nix_fod_swh_checker.swhid import SWHIdentifyError


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


def test_check_fod_flat_unknown(monkeypatch):
    fod = make_fod(method="flat", hash_algo="sha256", hash_hex="b" * 64)
    client = FakeSWHClient(content_known=False)
    monkeypatch.setattr(checker_module, "try_disarchive", lambda *a, **k: None)
    result = check_fod(fod, client)
    assert result.known is False
    assert result.method == SWHLookupMethod.CONTENT_HASH


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


def test_check_fod_git_method_unknown(monkeypatch):
    fod = make_fod(method="git", hash_algo="sha1", hash_hex="d" * 40)
    client = FakeSWHClient()
    monkeypatch.setattr(checker_module, "try_disarchive", lambda *a, **k: None)
    result = check_fod(fod, client)
    assert result.known is False
    assert result.method == SWHLookupMethod.SWHID_KNOWN


def test_check_fod_nar_method_builds_and_identifies(monkeypatch):
    fod = make_fod(method="nar", hash_algo="sha256", hash_hex="e" * 64)
    swhid = "swh:1:dir:" + "f" * 40

    monkeypatch.setattr(
        checker_module, "realise_fod", lambda fod, *, nix_binary, on_log=None: "/nix/store/z"
    )
    monkeypatch.setattr(
        checker_module,
        "compute_swhid",
        lambda path, *, swh_binary, on_log=None: swhid if path == "/nix/store/z" else None,
    )

    client = FakeSWHClient(known_swhids={swhid: True})
    result = check_fod(fod, client)
    assert result.known is True
    assert result.method == SWHLookupMethod.BUILD_AND_IDENTIFY
    assert "/nix/store/z" in result.detail
    assert swhid in result.detail
    assert result.swhid == swhid
    assert result.swh_url == f"https://archive.softwareheritage.org/{swhid}"


def test_check_fod_nar_method_unknown_swhid(monkeypatch):
    fod = make_fod(method="nar", hash_algo="sha256", hash_hex="1" * 64)
    swhid = "swh:1:dir:" + "2" * 40

    monkeypatch.setattr(
        checker_module, "realise_fod", lambda fod, *, nix_binary, on_log=None: "/nix/store/z"
    )
    monkeypatch.setattr(
        checker_module, "compute_swhid", lambda path, *, swh_binary, on_log=None: swhid
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
    assert result.method == SWHLookupMethod.UNSUPPORTED
    assert "boom" in result.detail


def test_check_fod_nar_method_identify_failure_is_undetermined(monkeypatch):
    fod = make_fod(method="nar", hash_algo="sha256", hash_hex="4" * 64)

    monkeypatch.setattr(
        checker_module, "realise_fod", lambda fod, *, nix_binary, on_log=None: "/nix/store/z"
    )

    def fail_identify(path, *, swh_binary, on_log=None):
        raise SWHIdentifyError("boom")

    monkeypatch.setattr(checker_module, "compute_swhid", fail_identify)

    client = FakeSWHClient()
    result = check_fod(fod, client)
    assert result.known is None
    assert result.method == SWHLookupMethod.UNSUPPORTED
    assert "boom" in result.detail


def test_check_fod_flat_unknown_but_known_after_disarchive(monkeypatch):
    fod = make_fod(method="flat", hash_algo="sha256", hash_hex="b" * 64)
    swhid = "swh:1:dir:" + "c" * 40
    client = FakeSWHClient(content_known=False)

    def fake_try_disarchive(fod, client, *, nix_binary, swh_binary, on_log=None):
        return SWHCheckResult(
            fod=fod,
            known=True,
            method=SWHLookupMethod.KNOWN_AFTER_DISARCHIVE,
            detail=f"unpacked /nix/store/archive.tar.gz and computed {swhid}",
            swhid=swhid,
            swh_url=f"https://archive.softwareheritage.org/{swhid}",
        )

    monkeypatch.setattr(checker_module, "try_disarchive", fake_try_disarchive)
    result = check_fod(fod, client)
    assert result.known is True
    assert result.method == SWHLookupMethod.KNOWN_AFTER_DISARCHIVE
    assert result.swhid == swhid


def test_check_fod_git_unknown_but_known_after_disarchive(monkeypatch):
    fod = make_fod(method="git", hash_algo="sha1", hash_hex="d" * 40)
    swhid = "swh:1:dir:" + "e" * 40
    client = FakeSWHClient()

    def fake_try_disarchive(fod, client, *, nix_binary, swh_binary, on_log=None):
        return SWHCheckResult(
            fod=fod,
            known=True,
            method=SWHLookupMethod.KNOWN_AFTER_DISARCHIVE,
            detail=f"unpacked /nix/store/archive.tar.gz and computed {swhid}",
            swhid=swhid,
            swh_url=f"https://archive.softwareheritage.org/{swhid}",
        )

    monkeypatch.setattr(checker_module, "try_disarchive", fake_try_disarchive)
    result = check_fod(fod, client)
    assert result.known is True
    assert result.method == SWHLookupMethod.KNOWN_AFTER_DISARCHIVE
    assert result.swhid == swhid
