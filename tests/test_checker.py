from nix_fod_swh_checker.checker import check_fod
from nix_fod_swh_checker.models import FixedOutputDerivation, SWHLookupMethod
from nix_fod_swh_checker.swh import ContentLookupResult


class FakeSWHClient:
    def __init__(self, content_known=False, known_swhids=None, known_origins=None):
        self.content_known = content_known
        self.known_swhids = known_swhids or {}
        self.known_origins = known_origins or set()
        self.content_calls = []
        self.origin_calls = []

    def lookup_content(self, algo, hash_hex):
        self.content_calls.append((algo, hash_hex))
        return ContentLookupResult(known=self.content_known)

    def lookup_known_swhids(self, swhids):
        return {swhid: self.known_swhids.get(swhid, False) for swhid in swhids}

    def lookup_origin(self, url):
        self.origin_calls.append(url)
        return url in self.known_origins


def make_fod(**overrides):
    defaults = dict(
        drv_path="/nix/store/x.drv",
        output_name="out",
        output_path="/nix/store/y",
        name="x",
        method="flat",
        hash_algo="sha256",
        hash_hex="a" * 64,
        urls=[],
    )
    defaults.update(overrides)
    return FixedOutputDerivation(**defaults)


def test_check_fod_flat_known():
    fod = make_fod(method="flat", hash_algo="sha256", hash_hex="a" * 64)
    client = FakeSWHClient(content_known=True)
    result = check_fod(fod, client)
    assert result.known is True
    assert result.method == SWHLookupMethod.CONTENT_HASH
    assert client.content_calls == [("sha256", "a" * 64)]


def test_check_fod_flat_unknown():
    fod = make_fod(method="flat", hash_algo="sha256", hash_hex="b" * 64)
    client = FakeSWHClient(content_known=False)
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


def test_check_fod_git_method_unknown():
    fod = make_fod(method="git", hash_algo="sha1", hash_hex="d" * 40)
    client = FakeSWHClient()
    result = check_fod(fod, client)
    assert result.known is False
    assert result.method == SWHLookupMethod.SWHID_KNOWN


def test_check_fod_nar_method_falls_back_to_origin():
    fod = make_fod(
        method="nar",
        hash_algo="sha256",
        hash_hex="e" * 64,
        urls=["https://example.com/src.tar.gz"],
    )
    client = FakeSWHClient(known_origins={"https://example.com/src.tar.gz"})
    result = check_fod(fod, client)
    assert result.known is True
    assert result.method == SWHLookupMethod.ORIGIN_URL
    assert client.origin_calls == ["https://example.com/src.tar.gz"]


def test_check_fod_nar_method_no_urls_is_undetermined():
    fod = make_fod(method="nar", hash_algo="sha256", hash_hex="f" * 64, urls=[])
    client = FakeSWHClient()
    result = check_fod(fod, client)
    assert result.known is None
    assert result.method == SWHLookupMethod.UNSUPPORTED
