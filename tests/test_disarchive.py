import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from nix_archive_src import disarchive as disarchive_module
from nix_archive_src.disarchive import (
    DisarchiveError,
    DisarchiveTimeoutError,
    try_disarchive,
    unpack_archive,
)
from nix_archive_src.models import FixedOutputDerivation, SWHCheckResult, SWHLookupMethod
from nix_archive_src.nix import NixCommandError


# Directory SWHID for a tree containing a single file ``file.txt`` with the
# bytes ``b"hello"``, as computed by ``swh identify --no-filename``.
KNOWN_DIRECTORY_SWHID = "swh:1:dir:952dd0a0ff0d34ef3f52035c658e1d1ed56fd0c1"


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


def _make_disarchive_spec(top_dir: str, swhid: str) -> str:
    """Return a minimal disarchive database spec with the given top dir and SWHID."""
    return f"""(disarchive
  (version 0)
  (gzip-member
    (name "{top_dir}.tar.gz")
    (input (tarball
             (name "{top_dir}.tar")
             (headers
               ("{top_dir}/"
                (mode 493)
                (typeflag 53))
               ("{top_dir}/file.txt"
                (size 5)))
             (input (directory-ref
                      (version 0)
                      (name "{top_dir}")
                      (addresses
                        (swhid "{swhid}"))
                      (digest
                        (sha256
                          "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"))))))))
"""


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


def test_try_disarchive_unknown_directory_swhid(monkeypatch, tmp_path):
    archive = _make_tar_archive(tmp_path, [("src/file.txt", "hello")])

    monkeypatch.setattr(
        disarchive_module, "realise_fod", lambda fod, *, nix_binary, on_log=None: str(archive)
    )

    client = FakeSWHClient()
    result = try_disarchive(make_fod(), client)
    assert isinstance(result, SWHCheckResult)
    assert result.known is False
    assert result.method == SWHLookupMethod.KNOWN_AFTER_DISARCHIVE
    assert result.swhid == KNOWN_DIRECTORY_SWHID
    assert result.swh_url is None
    # The client was asked about the real directory SWHID.
    assert client.known_calls == [[KNOWN_DIRECTORY_SWHID]]


def test_try_disarchive_known_directory_swhid(monkeypatch, tmp_path):
    archive = _make_tar_archive(tmp_path, [("src/file.txt", "hello")])

    monkeypatch.setattr(
        disarchive_module, "realise_fod", lambda fod, *, nix_binary, on_log=None: str(archive)
    )

    client = FakeSWHClient(known_swhids={KNOWN_DIRECTORY_SWHID: True})
    result = try_disarchive(make_fod(), client)
    assert isinstance(result, SWHCheckResult)
    assert result.known is True
    assert result.method == SWHLookupMethod.KNOWN_AFTER_DISARCHIVE
    # The stripped SWHID is known, so it is reported unless disarchive's own
    # SWHID is also known.
    assert result.swhid == KNOWN_DIRECTORY_SWHID
    assert result.swh_url == f"https://archive.softwareheritage.org/{KNOWN_DIRECTORY_SWHID}"
    assert result.disarchive_spec is not None
    assert result.disarchive_spec.strip()
    assert result.disarchive_top_dir == "src"


def test_try_disarchive_disarchive_failure_is_undetermined(monkeypatch, tmp_path):
    archive = _make_tar_archive(tmp_path, [("src/file.txt", "hello")])

    monkeypatch.setattr(
        disarchive_module, "realise_fod", lambda fod, *, nix_binary, on_log=None: str(archive)
    )

    client = FakeSWHClient(known_swhids={KNOWN_DIRECTORY_SWHID: True})
    result = try_disarchive(
        make_fod(),
        client,
        disarchive_binary="does-not-exist",
    )
    assert isinstance(result, SWHCheckResult)
    assert result.known is None
    assert result.method == SWHLookupMethod.UNDETERMINED
    assert result.swhid == KNOWN_DIRECTORY_SWHID
    assert "disarchive failed" in result.detail


def test_try_disarchive_reuses_database_spec_when_stripped_known(monkeypatch, tmp_path):
    """When the database returns a spec whose embedded SWHID is unknown but the
    stripped directory is known, the fetched spec is reused and disarchive is
    not invoked locally.
    """
    spec = _make_disarchive_spec("src", "swh:1:dir:0000000000000000000000000000000000000000")

    class FakeResponse:
        status_code = 200
        text = spec

    monkeypatch.setattr(
        disarchive_module.requests, "get", lambda url, timeout=None: FakeResponse()
    )

    archive = _make_tar_archive(tmp_path, [("src/file.txt", "hello")])

    monkeypatch.setattr(
        disarchive_module, "realise_fod", lambda fod, *, nix_binary, on_log=None: str(archive)
    )

    disassemble_called = []
    original_disassemble = disarchive_module.disassemble_archive

    def capturing_disassemble(archive_path, **kwargs):
        disassemble_called.append(archive_path)
        return original_disassemble(archive_path, **kwargs)

    monkeypatch.setattr(disarchive_module, "disassemble_archive", capturing_disassemble)

    client = FakeSWHClient(known_swhids={KNOWN_DIRECTORY_SWHID: True})
    result = try_disarchive(make_fod(), client)
    assert isinstance(result, SWHCheckResult)
    assert result.known is True
    assert result.method == SWHLookupMethod.KNOWN_AFTER_DISARCHIVE
    assert result.swhid == KNOWN_DIRECTORY_SWHID
    assert result.disarchive_spec == spec
    assert result.disarchive_top_dir == "src"
    assert not disassemble_called


def test_try_disarchive_database_short_circuits_local_work(monkeypatch, tmp_path):
    spec = _make_disarchive_spec("src", KNOWN_DIRECTORY_SWHID)

    class FakeResponse:
        status_code = 200
        text = spec

    monkeypatch.setattr(
        disarchive_module.requests, "get", lambda url, timeout=None: FakeResponse()
    )

    # realise_fod should not be called when the database lookup succeeds.
    realised = []

    def fail_if_realised(fod, *, nix_binary, on_log=None):
        realised.append(fod)
        raise RuntimeError("realise_fod should not be called")

    monkeypatch.setattr(disarchive_module, "realise_fod", fail_if_realised)

    client = FakeSWHClient(known_swhids={KNOWN_DIRECTORY_SWHID: True})
    result = try_disarchive(
        make_fod(),
        client,
        disarchive_db_url="https://example.com/disarchive",
    )
    assert isinstance(result, SWHCheckResult)
    assert result.known is True
    assert result.method == SWHLookupMethod.KNOWN_AFTER_DISARCHIVE
    assert result.swhid == KNOWN_DIRECTORY_SWHID
    assert result.disarchive_swhid == KNOWN_DIRECTORY_SWHID
    assert result.disarchive_spec == spec
    assert result.disarchive_top_dir == "src"
    assert not realised


def test_try_disarchive_database_unknown_swhid_falls_back(monkeypatch, tmp_path):
    spec = _make_disarchive_spec("src", KNOWN_DIRECTORY_SWHID)

    class FakeResponse:
        status_code = 200
        text = spec

    monkeypatch.setattr(
        disarchive_module.requests, "get", lambda url, timeout=None: FakeResponse()
    )

    fallback = {}

    def fake_local(fod, client, **kwargs):
        fallback["kwargs"] = kwargs
        return SWHCheckResult(
            fod=fod,
            known=False,
            method=SWHLookupMethod.KNOWN_AFTER_DISARCHIVE,
            detail="local fallback",
            swhid=KNOWN_DIRECTORY_SWHID,
        )

    monkeypatch.setattr(disarchive_module, "_try_disarchive_local", fake_local)

    client = FakeSWHClient()
    result = try_disarchive(make_fod(), client)
    assert isinstance(result, SWHCheckResult)
    assert result.known is False
    assert result.method == SWHLookupMethod.KNOWN_AFTER_DISARCHIVE
    assert result.swhid == KNOWN_DIRECTORY_SWHID
    assert fallback["kwargs"]["db_spec"] == spec
    assert fallback["kwargs"]["db_top_dir"] == "src"


def test_try_disarchive_database_missing_entry_falls_back(monkeypatch, tmp_path):
    class FakeResponse:
        status_code = 404
        text = ""

    monkeypatch.setattr(
        disarchive_module.requests, "get", lambda url, timeout=None: FakeResponse()
    )

    monkeypatch.setattr(
        disarchive_module,
        "_try_disarchive_local",
        lambda fod, client, **kwargs: SWHCheckResult(
            fod=fod,
            known=False,
            method=SWHLookupMethod.KNOWN_AFTER_DISARCHIVE,
            detail="local fallback",
            swhid=KNOWN_DIRECTORY_SWHID,
        ),
    )

    client = FakeSWHClient()
    result = try_disarchive(make_fod(), client)
    assert isinstance(result, SWHCheckResult)
    assert result.known is False
    assert result.method == SWHLookupMethod.KNOWN_AFTER_DISARCHIVE
    assert result.swhid == KNOWN_DIRECTORY_SWHID


def test_try_disarchive_database_request_error_falls_back(monkeypatch, tmp_path):
    import requests as requests_lib

    def fail_request(url, timeout=None):
        raise requests_lib.RequestException("network down")

    monkeypatch.setattr(disarchive_module.requests, "get", fail_request)

    monkeypatch.setattr(
        disarchive_module,
        "_try_disarchive_local",
        lambda fod, client, **kwargs: SWHCheckResult(
            fod=fod,
            known=False,
            method=SWHLookupMethod.KNOWN_AFTER_DISARCHIVE,
            detail="local fallback",
            swhid=KNOWN_DIRECTORY_SWHID,
        ),
    )

    client = FakeSWHClient()
    result = try_disarchive(make_fod(), client)
    assert isinstance(result, SWHCheckResult)
    assert result.known is False
    assert result.method == SWHLookupMethod.KNOWN_AFTER_DISARCHIVE


def test_try_disarchive_database_no_hash_falls_back(monkeypatch, tmp_path):
    spec = _make_disarchive_spec("src", KNOWN_DIRECTORY_SWHID)

    class FakeResponse:
        status_code = 200
        text = spec

    monkeypatch.setattr(
        disarchive_module.requests, "get", lambda url, timeout=None: FakeResponse()
    )

    monkeypatch.setattr(
        disarchive_module,
        "_try_disarchive_local",
        lambda fod, client, **kwargs: SWHCheckResult(
            fod=fod,
            known=True,
            method=SWHLookupMethod.KNOWN_AFTER_DISARCHIVE,
            detail="local fallback",
            swhid=KNOWN_DIRECTORY_SWHID,
        ),
    )

    client = FakeSWHClient(known_swhids={KNOWN_DIRECTORY_SWHID: True})
    fod = make_fod(hash_algo=None, hash_hex=None)
    result = try_disarchive(fod, client)
    assert isinstance(result, SWHCheckResult)
    assert result.known is True
    assert result.method == SWHLookupMethod.KNOWN_AFTER_DISARCHIVE


def test_try_disarchive_database_spec_without_swhid_falls_back(monkeypatch, tmp_path):
    class FakeResponse:
        status_code = 200
        text = "(disarchive (version 0))"

    monkeypatch.setattr(
        disarchive_module.requests, "get", lambda url, timeout=None: FakeResponse()
    )

    monkeypatch.setattr(
        disarchive_module,
        "_try_disarchive_local",
        lambda fod, client, **kwargs: SWHCheckResult(
            fod=fod,
            known=True,
            method=SWHLookupMethod.KNOWN_AFTER_DISARCHIVE,
            detail="local fallback",
            swhid=KNOWN_DIRECTORY_SWHID,
        ),
    )

    client = FakeSWHClient(known_swhids={KNOWN_DIRECTORY_SWHID: True})
    result = try_disarchive(make_fod(), client)
    assert isinstance(result, SWHCheckResult)
    assert result.known is True
    assert result.method == SWHLookupMethod.KNOWN_AFTER_DISARCHIVE


def test_try_disarchive_undetermined_when_spec_is_invalid_false(monkeypatch, tmp_path):
    """If disarchive returns the ``#f`` blueprint, the result is undetermined."""
    archive = _make_tar_archive(tmp_path, [("src/file.txt", "hello")])

    monkeypatch.setattr(
        disarchive_module, "realise_fod", lambda fod, *, nix_binary, on_log=None: str(archive)
    )

    def fake_disassemble(archive_path, **kwargs):
        return "(disarchive (version 0) #f)\n"

    monkeypatch.setattr(disarchive_module, "disassemble_archive", fake_disassemble)

    client = FakeSWHClient(known_swhids={KNOWN_DIRECTORY_SWHID: True})
    result = try_disarchive(make_fod(), client)
    assert isinstance(result, SWHCheckResult)
    assert result.known is None
    assert result.method == SWHLookupMethod.UNDETERMINED
    assert result.swhid == KNOWN_DIRECTORY_SWHID
    assert "could not capture a usable spec" in result.detail


def test_try_disarchive_caches_invalid_local_spec_as_miss(monkeypatch, tmp_path):
    """An invalid spec produced by local disarchive is cached as a miss."""
    import subprocess as subprocess_module

    from nix_archive_src.cache import Cache

    archive = _make_tar_archive(tmp_path, [("src/file.txt", "hello")])
    cache = Cache(tmp_path / "cache.json")

    monkeypatch.setattr(
        disarchive_module, "realise_fod", lambda fod, *, nix_binary, on_log=None: str(archive)
    )

    calls = []
    original_subprocess_run = disarchive_module.subprocess.run

    def fake_subprocess_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1] == "identify":
            # Let the real ``swh identify`` tool run; it is available in the
            # test environment and produces the stripped directory SWHID.
            return original_subprocess_run(cmd, **kwargs)
        if cmd[1] == "-c":
            # Unpacking subprocess; let it run normally.
            return original_subprocess_run(cmd, **kwargs)
        # Write the invalid disarchive spec to the output file requested by
        # ``disarchive disassemble`` so the real caching path is exercised.
        assert cmd[1] == "disassemble"
        spec_path = cmd[cmd.index("-o") + 1]
        Path(spec_path).write_text("(disarchive (version 0) #f)\n")
        return subprocess_module.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(disarchive_module.subprocess, "run", fake_subprocess_run)

    client = FakeSWHClient(known_swhids={KNOWN_DIRECTORY_SWHID: True})
    fod = make_fod()
    result1 = try_disarchive(fod, client, cache=cache)
    assert result1.known is None
    assert result1.method == SWHLookupMethod.UNDETERMINED
    # One call for unpacking, one for ``swh identify``, and one for
    # ``disarchive disassemble``.
    assert len(calls) == 3

    cache.save()
    cache2 = Cache(tmp_path / "cache.json")
    result2 = try_disarchive(fod, client, cache=cache2)
    assert result2.known is None
    assert result2.method == SWHLookupMethod.UNDETERMINED
    # The cached invalid spec must prevent a second ``disarchive disassemble``.
    assert len(calls) == 3


def test_try_disarchive_caches_disarchive_timeout_as_miss(monkeypatch, tmp_path):
    """A disarchive timeout is cached as a miss and reused with lower timeouts."""
    import subprocess as subprocess_module

    from nix_archive_src.cache import Cache

    archive = _make_tar_archive(tmp_path, [("src/file.txt", "hello")])
    cache = Cache(tmp_path / "cache.json")

    monkeypatch.setattr(
        disarchive_module, "realise_fod", lambda fod, *, nix_binary, on_log=None: str(archive)
    )

    calls = []
    original_subprocess_run = disarchive_module.subprocess.run

    def fake_subprocess_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1] == "identify":
            return original_subprocess_run(cmd, **kwargs)
        if cmd[1] == "-c":
            return original_subprocess_run(cmd, **kwargs)
        assert cmd[1] == "disassemble"
        raise subprocess_module.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(disarchive_module.subprocess, "run", fake_subprocess_run)

    client = FakeSWHClient(known_swhids={KNOWN_DIRECTORY_SWHID: True})
    fod = make_fod()
    result1 = try_disarchive(fod, client, cache=cache, disarchive_timeout=1.0)
    assert result1.known is None
    assert result1.method == SWHLookupMethod.UNDETERMINED
    assert "timed out" in result1.detail
    # One call for unpacking, one for ``swh identify``, and one for
    # ``disarchive disassemble``.
    assert len(calls) == 3

    cache.save()
    cache2 = Cache(tmp_path / "cache.json")
    result2 = try_disarchive(fod, client, cache=cache2, disarchive_timeout=0.5)
    assert result2.known is None
    assert result2.method == SWHLookupMethod.UNDETERMINED
    assert "timed out" in result2.detail
    # The cached timeout must prevent a second ``disarchive disassemble``.
    assert len(calls) == 3


def test_try_disarchive_ignores_cached_disarchive_timeout_when_increased(monkeypatch, tmp_path):
    """A cached disarchive timeout is ignored when the timeout is increased."""
    import subprocess as subprocess_module

    from nix_archive_src.cache import Cache

    archive = _make_tar_archive(tmp_path, [("src/file.txt", "hello")])
    cache = Cache(tmp_path / "cache.json")

    monkeypatch.setattr(
        disarchive_module, "realise_fod", lambda fod, *, nix_binary, on_log=None: str(archive)
    )

    calls = []
    original_subprocess_run = disarchive_module.subprocess.run

    def fake_subprocess_run(cmd, **kwargs):
        calls.append((cmd[1], kwargs.get("timeout")))
        if cmd[1] == "identify":
            return original_subprocess_run(cmd, **kwargs)
        if cmd[1] == "-c":
            return original_subprocess_run(cmd, **kwargs)
        assert cmd[1] == "disassemble"
        if kwargs["timeout"] <= 1.0:
            raise subprocess_module.TimeoutExpired(cmd, kwargs["timeout"])
        # Return a valid spec on the second call with the higher timeout.
        spec_path = cmd[cmd.index("-o") + 1]
        Path(spec_path).write_text(
            _make_disarchive_spec("src", KNOWN_DIRECTORY_SWHID)
        )
        return subprocess_module.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(disarchive_module.subprocess, "run", fake_subprocess_run)

    client = FakeSWHClient(known_swhids={KNOWN_DIRECTORY_SWHID: True})
    fod = make_fod()
    result1 = try_disarchive(fod, client, cache=cache, disarchive_timeout=1.0)
    assert result1.known is None
    assert result1.method == SWHLookupMethod.UNDETERMINED
    # One call for unpacking, one for ``swh identify``, and one for
    # ``disarchive disassemble``.
    assert len(calls) == 3

    cache.save()
    cache2 = Cache(tmp_path / "cache.json")
    result2 = try_disarchive(fod, client, cache=cache2, disarchive_timeout=2.0)
    assert result2.known is True
    assert result2.method == SWHLookupMethod.KNOWN_AFTER_DISARCHIVE
    # Unpacking and ``swh identify`` are still cached, so only
    # ``disarchive disassemble`` runs again.
    assert len(calls) == 5


def test_unpack_archive_timeout_is_applied(monkeypatch, tmp_path):
    """The timeout is passed to the subprocess used for unpacking."""
    archive = _make_tar_archive(tmp_path, [("src/file.txt", "hello")])
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(kwargs.get("timeout"))
        # Return a completed process without actually extracting anything.
        import subprocess as subprocess_module

        return subprocess_module.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(disarchive_module.subprocess, "run", fake_run)
    unpack_archive(str(archive), timeout=5.0)
    assert calls == [5.0]


def test_unpack_archive_timeout_is_cached_as_miss(monkeypatch, tmp_path):
    """A timeout during unpacking is cached and reused with lower timeouts."""
    from nix_archive_src.cache import Cache

    archive = _make_tar_archive(tmp_path, [("src/file.txt", "hello")])
    cache = Cache(tmp_path / "cache.json")

    def fake_run(cmd, **kwargs):
        import subprocess as subprocess_module

        raise subprocess_module.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(disarchive_module.subprocess, "run", fake_run)

    with pytest.raises(DisarchiveTimeoutError):
        unpack_archive(str(archive), timeout=1.0, cache=cache, cache_key="test")

    cache.save()
    cache2 = Cache(tmp_path / "cache.json")
    calls = []

    def counting_run(cmd, **kwargs):
        calls.append(kwargs.get("timeout"))
        import subprocess as subprocess_module

        raise subprocess_module.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(disarchive_module.subprocess, "run", counting_run)
    with pytest.raises(DisarchiveTimeoutError):
        unpack_archive(str(archive), timeout=0.5, cache=cache2, cache_key="test")
    assert calls == []


def test_unpack_archive_ignores_cached_timeout_when_increased(monkeypatch, tmp_path):
    """A cached unpack timeout is ignored when the timeout is increased."""
    from nix_archive_src.cache import Cache

    archive = _make_tar_archive(tmp_path, [("src/file.txt", "hello")])
    cache = Cache(tmp_path / "cache.json")

    def fake_run(cmd, **kwargs):
        import subprocess as subprocess_module

        raise subprocess_module.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(disarchive_module.subprocess, "run", fake_run)

    with pytest.raises(DisarchiveTimeoutError):
        unpack_archive(str(archive), timeout=1.0, cache=cache, cache_key="test")

    cache.save()
    cache2 = Cache(tmp_path / "cache.json")
    calls = []

    def counting_run(cmd, **kwargs):
        calls.append(kwargs.get("timeout"))
        import subprocess as subprocess_module

        raise subprocess_module.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(disarchive_module.subprocess, "run", counting_run)
    with pytest.raises(DisarchiveTimeoutError):
        unpack_archive(str(archive), timeout=2.0, cache=cache2, cache_key="test")
    assert calls == [2.0]


def test_try_disarchive_unpack_timeout_is_undetermined(monkeypatch, tmp_path):
    """When unpacking times out, the result is reported as undetermined."""
    import subprocess as subprocess_module

    archive = _make_tar_archive(tmp_path, [("src/file.txt", "hello")])

    monkeypatch.setattr(
        disarchive_module, "realise_fod", lambda fod, *, nix_binary, on_log=None: str(archive)
    )

    def fake_run(cmd, **kwargs):
        raise subprocess_module.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(disarchive_module.subprocess, "run", fake_run)

    client = FakeSWHClient()
    result = try_disarchive(make_fod(), client, unpack_timeout=1.0)
    assert isinstance(result, SWHCheckResult)
    assert result.known is None
    assert result.method == SWHLookupMethod.UNDETERMINED
    assert "unpacking" in result.detail
    assert "timed out" in result.detail


def test_try_disarchive_skip_disarchive_skips_database_lookup(monkeypatch, tmp_path):
    db_called = []

    def fail_if_called(url, timeout=None):
        db_called.append(url)
        raise RuntimeError("disarchive database should not be queried")

    monkeypatch.setattr(disarchive_module.requests, "get", fail_if_called)

    monkeypatch.setattr(
        disarchive_module,
        "_try_disarchive_local",
        lambda fod, client, **kwargs: SWHCheckResult(
            fod=fod,
            known=False,
            method=SWHLookupMethod.KNOWN_AFTER_DISARCHIVE,
            detail="local fallback",
            swhid=KNOWN_DIRECTORY_SWHID,
        ),
    )

    client = FakeSWHClient()
    result = try_disarchive(make_fod(), client, skip_disarchive=True)
    assert isinstance(result, SWHCheckResult)
    assert not db_called
    assert result.known is False
    assert result.method == SWHLookupMethod.KNOWN_AFTER_DISARCHIVE


def _default_fod_cache_prefix(fod):
    """Return the cache key prefix used by default for ``make_fod()`` FODs."""
    return f"{fod.hash_algo}:{fod.hash_hex}"


def test_try_disarchive_local_uses_cache_to_skip_realisation(monkeypatch, tmp_path):
    """When the stripped SWHID and disarchive spec are cached, no realisation."""
    from nix_archive_src.cache import Cache

    cache = Cache(tmp_path / "cache.json")
    fod = make_fod(output_path="/nix/store/cached-archive.tar.gz")
    prefix = _default_fod_cache_prefix(fod)
    cache.set(
        f"tool:swh_identify:{prefix}:stripped",
        {"swhid": KNOWN_DIRECTORY_SWHID},
        is_miss=False,
    )
    spec = _make_disarchive_spec("src", KNOWN_DIRECTORY_SWHID)
    cache.set(
        f"tool:disassemble:{prefix}:disassemble",
        {"spec": spec},
        is_miss=False,
    )

    realised = []

    def fail_if_realised(fod, *, nix_binary, on_log=None):
        realised.append(fod)
        raise RuntimeError("realise_fod should not be called")

    monkeypatch.setattr(disarchive_module, "realise_fod", fail_if_realised)

    client = FakeSWHClient(known_swhids={KNOWN_DIRECTORY_SWHID: True})
    result = try_disarchive(fod, client, cache=cache)
    assert isinstance(result, SWHCheckResult)
    assert not realised
    assert result.known is True
    assert result.method == SWHLookupMethod.KNOWN_AFTER_DISARCHIVE
    assert result.swhid == KNOWN_DIRECTORY_SWHID
    assert result.disarchive_spec == spec
    assert result.disarchive_top_dir == "src"


def test_try_disarchive_local_cache_unknown_stripped_swhid_skips_realisation(
    monkeypatch, tmp_path
):
    """A cached stripped SWHID that is not known lets us return UNKNOWN immediately."""
    from nix_archive_src.cache import Cache

    cache = Cache(tmp_path / "cache.json")
    fod = make_fod(output_path="/nix/store/cached-archive.tar.gz")
    prefix = _default_fod_cache_prefix(fod)
    cache.set(
        f"tool:swh_identify:{prefix}:stripped",
        {"swhid": KNOWN_DIRECTORY_SWHID},
        is_miss=False,
    )

    realised = []

    def fail_if_realised(fod, *, nix_binary, on_log=None):
        realised.append(fod)
        raise RuntimeError("realise_fod should not be called")

    monkeypatch.setattr(disarchive_module, "realise_fod", fail_if_realised)

    client = FakeSWHClient()
    result = try_disarchive(fod, client, cache=cache)
    assert isinstance(result, SWHCheckResult)
    assert not realised
    assert result.known is False
    assert result.method == SWHLookupMethod.KNOWN_AFTER_DISARCHIVE
    assert result.swhid == KNOWN_DIRECTORY_SWHID


def test_try_disarchive_local_cache_falls_back_to_output_path(monkeypatch, tmp_path):
    """When the FOD has no hash, the cache key falls back to the output path."""
    from nix_archive_src.cache import Cache

    cache = Cache(tmp_path / "cache.json")
    output_path = "/nix/store/cached-archive.tar.gz"
    cache.set(
        f"tool:swh_identify:{output_path}:stripped",
        {"swhid": KNOWN_DIRECTORY_SWHID},
        is_miss=False,
    )

    realised = []

    def fail_if_realised(fod, *, nix_binary, on_log=None):
        realised.append(fod)
        raise RuntimeError("realise_fod should not be called")

    monkeypatch.setattr(disarchive_module, "realise_fod", fail_if_realised)

    fod = make_fod(output_path=output_path, hash_algo=None, hash_hex=None)
    client = FakeSWHClient()
    result = try_disarchive(fod, client, cache=cache)
    assert isinstance(result, SWHCheckResult)
    assert not realised
    assert result.known is False
    assert result.method == SWHLookupMethod.KNOWN_AFTER_DISARCHIVE
    assert result.swhid == KNOWN_DIRECTORY_SWHID


def test_try_disarchive_local_cache_uses_hash_not_output_path(monkeypatch, tmp_path):
    """The cache key is based on the FOD hash, so the same content shares entries."""
    from nix_archive_src.cache import Cache

    cache = Cache(tmp_path / "cache.json")
    # Populate the cache using one output path.
    first_fod = make_fod(output_path="/nix/store/first-archive.tar.gz")
    prefix = _default_fod_cache_prefix(first_fod)
    cache.set(
        f"tool:swh_identify:{prefix}:stripped",
        {"swhid": KNOWN_DIRECTORY_SWHID},
        is_miss=False,
    )

    realised = []

    def fail_if_realised(fod, *, nix_binary, on_log=None):
        realised.append(fod)
        raise RuntimeError("realise_fod should not be called")

    monkeypatch.setattr(disarchive_module, "realise_fod", fail_if_realised)

    # A different FOD with the same hash should reuse the cached stripped SWHID.
    second_fod = make_fod(output_path="/nix/store/second-archive.tar.gz")
    client = FakeSWHClient()
    result = try_disarchive(second_fod, client, cache=cache)
    assert isinstance(result, SWHCheckResult)
    assert not realised
    assert result.known is False
    assert result.method == SWHLookupMethod.KNOWN_AFTER_DISARCHIVE
    assert result.swhid == KNOWN_DIRECTORY_SWHID
