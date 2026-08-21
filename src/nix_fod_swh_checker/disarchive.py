"""Look up and decompress archive metadata for FODs.

When a FOD output is a single file (``method="flat"`` or ``method="git"`` as
a blob) that is not known to Software Heritage as a content object, it may
still be an archive whose *contents* are archived as a directory. This module
first tries the GNU Guix disarchive database as a fast cache; if that fails,
it realises the FOD, unpacks the archive with standard tools, and captures a
fresh ``disarchive`` specification.

``disarchive`` computes its own directory SWHID, which may include a single
top-level directory that Nix normally strips. We therefore keep track of two
SWHIDs:

- the *stripped* SWHID, computed from the tree after applying Nix's
  ``stripHash`` semantics; and
- the *disarchive* SWHID, embedded by ``disarchive`` in its specification.

The stripped SWHID is more likely to be archived by Software Heritage, but
rebuilding the original archive requires the disarchive SWHID. When only the
stripped SWHID is known, we can still reconstruct the archive by wrapping the
stripped directory back inside its original top-level directory before calling
``disarchive assemble``.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Callable

import requests

from .models import FixedOutputDerivation, SWHCheckResult, SWHLookupMethod
from .nix import NixCommandError, realise_fod
from .swh import SWHClient
from .swhid import SWHIdentifyError, compute_swhid

_ARCHIVE_URL = "https://archive.softwareheritage.org"
_DISARCHIVE_DB_URL = "https://disarchive.guix.gnu.org"


class DisarchiveError(RuntimeError):
    """Raised when an archive cannot be decompressed or disassembled."""


class DisarchiveTimeoutError(DisarchiveError):
    """Raised when ``disarchive disassemble`` exceeds its timeout."""


def try_disarchive(
    fod: FixedOutputDerivation,
    client: SWHClient,
    *,
    nix_binary: str = "nix",
    swh_binary: str = "swh",
    disarchive_binary: str = "disarchive",
    swh_identify_timeout: float = 30.0,
    disarchive_timeout: float = 30.0,
    disarchive_db_url: str = _DISARCHIVE_DB_URL,
    skip_disarchive: bool = False,
    on_log: Callable[[str], None] | None = None,
) -> SWHCheckResult | None:
    """Check a FOD's archive contents, using the disarchive database as a cache.

    Returns a :class:`SWHCheckResult` when the archive contents were looked up
    successfully, or ``None`` when the FOD could not be realised, is not an
    archive, or could not be unpacked.

    Unless ``skip_disarchive`` is ``True``, the GNU Guix disarchive database is
    queried by the FOD's hash first. If it returns a specification, the
    embedded directory SWHID is checked against Software Heritage. When that
    SWHID is known, the result is reported immediately without realising the
    FOD or invoking the local ``disarchive`` tool.

    If the database lookup is skipped, has no entry, the embedded SWHID is
    unknown, or the database request fails, the function falls back to
    realising the FOD, unpacking the archive, and running ``disarchive
    disassemble`` locally. When the database returned a spec but its embedded
    SWHID is unknown, that spec is still passed to the local path so that
    ``disarchive disassemble`` can be skipped if the stripped directory SWHID
    is known.
    """
    if skip_disarchive:
        db_result: SWHCheckResult | None = None
        db_spec: str | None = None
        db_top_dir: str | None = None
        if on_log:
            on_log(f"{fod.label}: skipping disarchive database lookup")
    else:
        db_result, db_spec, db_top_dir = _try_disarchive_database(
            fod,
            client,
            db_url=disarchive_db_url,
            on_log=on_log,
        )
    if db_result is not None:
        return db_result

    return _try_disarchive_local(
        fod,
        client,
        nix_binary=nix_binary,
        swh_binary=swh_binary,
        disarchive_binary=disarchive_binary,
        swh_identify_timeout=swh_identify_timeout,
        disarchive_timeout=disarchive_timeout,
        db_spec=db_spec,
        db_top_dir=db_top_dir,
        on_log=on_log,
    )


def _try_disarchive_database(
    fod: FixedOutputDerivation,
    client: SWHClient,
    *,
    db_url: str = _DISARCHIVE_DB_URL,
    timeout: float = 20.0,
    on_log: Callable[[str], None] | None = None,
) -> tuple[SWHCheckResult | None, str | None, str | None]:
    """Query the disarchive database by FOD hash.

    Returns a tuple ``(result, spec, top_dir)``. ``result`` is a
    :class:`SWHCheckResult` when the database lookup succeeded and the embedded
    SWHID is known. ``spec`` and ``top_dir`` are non-``None`` when the database
    returned a usable specification, even if its embedded SWHID is unknown; the
    caller can reuse them to skip ``disarchive disassemble`` in the local path.

    When the FOD hash cannot be used, the database has no entry, or the lookup
    fails, all three returned values are ``None``.
    """
    if not fod.hash_algo or not fod.hash_hex:
        return None, None, None

    url = f"{db_url}/{fod.hash_algo}/{fod.hash_hex}"
    if on_log:
        on_log(f"{fod.label}: checking disarchive database at {url}...")

    try:
        response = requests.get(url, timeout=timeout)
    except requests.RequestException as exc:
        if on_log:
            on_log(f"{fod.label}: disarchive database lookup failed: {exc}")
        return None, None, None

    if response.status_code == 404:
        if on_log:
            on_log(f"{fod.label}: not found in disarchive database")
        return None, None, None
    if response.status_code != 200:
        if on_log:
            on_log(
                f"{fod.label}: unexpected disarchive database status "
                f"{response.status_code}"
            )
        return None, None, None

    spec = response.text
    disarchive_swhid = _extract_disarchive_swhid(spec)
    if not disarchive_swhid:
        if on_log:
            on_log(f"{fod.label}: disarchive database spec contains no directory SWHID")
        return None, spec, None

    disarchive_known = (
        client.lookup_known_swhids([disarchive_swhid]).get(disarchive_swhid, False)
    )
    if not disarchive_known:
        if on_log:
            on_log(
                f"{fod.label}: disarchive database returned {disarchive_swhid}, "
                f"but it is not known to Software Heritage"
            )
        top_dir = _extract_disarchive_top_dir(spec)
        return None, spec, top_dir

    top_dir = _extract_disarchive_top_dir(spec)

    if on_log:
        on_log(
            f"{fod.label}: disarchive database returned known {disarchive_swhid}"
        )

    return (
        SWHCheckResult(
            fod=fod,
            known=True,
            method=SWHLookupMethod.KNOWN_AFTER_DISARCHIVE,
            detail=f"disarchive database returned {disarchive_swhid}",
            swhid=disarchive_swhid,
            swh_url=f"{_ARCHIVE_URL}/{disarchive_swhid}",
            disarchive_spec=spec,
            disarchive_swhid=disarchive_swhid,
            disarchive_top_dir=top_dir,
        ),
        spec,
        top_dir,
    )


def _try_disarchive_local(
    fod: FixedOutputDerivation,
    client: SWHClient,
    *,
    nix_binary: str = "nix",
    swh_binary: str = "swh",
    disarchive_binary: str = "disarchive",
    swh_identify_timeout: float = 30.0,
    disarchive_timeout: float = 30.0,
    db_spec: str | None = None,
    db_top_dir: str | None = None,
    on_log: Callable[[str], None] | None = None,
) -> SWHCheckResult | None:
    """Realise a FOD, try to unpack it, and check its directory SWHID locally.

    Returns a :class:`SWHCheckResult` when the archive was successfully
    unpacked and its directory SWHID looked up, or ``None`` when the FOD could
    not be realised, is not an archive, or could not be unpacked.

    The archive is unpacked and its stripped directory SWHID is checked first.
    If that SWHID is not known to Software Heritage, the archive contents are
    not archived and there is no point in capturing a disarchive specification,
    so the slow ``disarchive disassemble`` step is skipped. If the stripped
    SWHID is known but ``disarchive disassemble`` fails or times out, the
    result is reported as ``UNDETERMINED`` so the user knows the specification
    is missing.

    If ``db_spec`` is provided (for example from the disarchive database),
    ``disarchive disassemble`` is skipped and the provided specification is
    used directly. In that case ``db_top_dir`` is used as ``disarchive_top_dir``
    when it cannot be extracted from ``db_spec``.
    """
    try:
        archive_path = realise_fod(fod, nix_binary=nix_binary, on_log=on_log)
    except NixCommandError:
        return None

    if not Path(archive_path).is_file():
        return None

    if on_log:
        on_log(f"unpacking {archive_path} to compute its directory SWHID...")

    try:
        unpacked_path = unpack_archive(archive_path)
    except DisarchiveError:
        return None

    # Archives that contain a single top-level directory are treated like
    # Nix's stripHash: the directory *inside* is the source tree.
    content_path = _single_top_level_directory(unpacked_path) or unpacked_path
    stripped_top_dir = (
        Path(content_path).name if content_path != unpacked_path else None
    )

    try:
        stripped_swhid = compute_swhid(
            content_path,
            swh_binary=swh_binary,
            on_log=on_log,
            timeout=swh_identify_timeout,
        )
    except SWHIdentifyError as exc:
        _cleanup(unpacked_path)
        if on_log:
            on_log(f"could not compute SWHID for {content_path}: {exc}")
        return SWHCheckResult(
            fod=fod,
            known=None,
            method=SWHLookupMethod.UNDETERMINED,
            detail=f"unpacked {archive_path} but could not compute its SWHID: {exc}",
        )

    stripped_known = client.lookup_known_swhids([stripped_swhid]).get(stripped_swhid, False)
    if not stripped_known:
        _cleanup(unpacked_path)
        return SWHCheckResult(
            fod=fod,
            known=False,
            method=SWHLookupMethod.KNOWN_AFTER_DISARCHIVE,
            detail=f"unpacked {archive_path} and computed {stripped_swhid}; contents not known",
            swhid=stripped_swhid,
        )

    # The stripped contents are known. If a database spec was already fetched,
    # use it directly to avoid the slow local disassemble step.
    if db_spec is not None:
        spec = db_spec
        disarchive_top_dir = _extract_disarchive_top_dir(spec) or db_top_dir
    else:
        try:
            spec = disassemble_archive(
                archive_path,
                disarchive_binary=disarchive_binary,
                timeout=disarchive_timeout,
                on_log=on_log,
            )
        except DisarchiveTimeoutError as exc:
            _cleanup(unpacked_path)
            if on_log:
                on_log(f"disarchive timed out for {archive_path}: {exc}")
            return SWHCheckResult(
                fod=fod,
                known=None,
                method=SWHLookupMethod.UNDETERMINED,
                detail=f"contents known as {stripped_swhid} but disarchive timed out before capturing the spec",
                swhid=stripped_swhid,
                swh_url=f"{_ARCHIVE_URL}/{stripped_swhid}",
            )
        except DisarchiveError as exc:
            # Without a disarchive specification we cannot reconstruct the exact
            # original archive, so the result is not usable for a SWH-backed FOD.
            _cleanup(unpacked_path)
            if on_log:
                on_log(f"disarchive failed for {archive_path}: {exc}")
            return SWHCheckResult(
                fod=fod,
                known=None,
                method=SWHLookupMethod.UNDETERMINED,
                detail=f"contents known as {stripped_swhid} but disarchive failed before capturing the spec",
                swhid=stripped_swhid,
                swh_url=f"{_ARCHIVE_URL}/{stripped_swhid}",
            )
        disarchive_top_dir = stripped_top_dir

    disarchive_swhid = _extract_disarchive_swhid(spec)
    disarchive_known = (
        client.lookup_known_swhids([disarchive_swhid]).get(disarchive_swhid, False)
        if disarchive_swhid
        else False
    )

    _cleanup(unpacked_path)

    # Prefer the disarchive SWHID for reporting when it is known, because that
    # is the directory disarchive can rebuild from directly. Otherwise report
    # the stripped SWHID, which is more likely to be archived.
    reported_swhid = disarchive_swhid if disarchive_known else stripped_swhid
    known = stripped_known or disarchive_known

    return SWHCheckResult(
        fod=fod,
        known=known,
        method=SWHLookupMethod.KNOWN_AFTER_DISARCHIVE,
        detail=f"unpacked {archive_path} and computed {stripped_swhid}",
        swhid=reported_swhid,
        swh_url=f"{_ARCHIVE_URL}/{reported_swhid}" if known else None,
        disarchive_spec=spec,
        disarchive_swhid=disarchive_swhid,
        disarchive_top_dir=disarchive_top_dir,
    )


def _extract_disarchive_swhid(spec: str) -> str | None:
    """Return the ``swh:1:dir:`` identifier embedded in a disarchive spec.

    The specification contains a single ``directory-ref`` with a list of
    addresses; we extract the SWHID address if present.
    """
    match = re.search(r'\(swhid\s+"(swh:1:dir:[a-f0-9]+)"\)', spec)
    return match.group(1) if match else None


def _extract_disarchive_top_dir(spec: str) -> str | None:
    """Return the name of the single top-level directory in a disarchive spec.

    The ``directory-ref`` form includes a ``name`` field that corresponds to
    the top-level directory Nix normally strips. We extract it so that, when
    only the stripped SWHID is known, the generated expression can re-wrap the
    directory before calling ``disarchive assemble``.
    """
    match = re.search(r'\(directory-ref\s+\(version\s+\d+\)\s+\(name\s+"([^"]+)"\)', spec)
    return match.group(1) if match else None


def disassemble_archive(
    archive_path: str,
    *,
    disarchive_binary: str = "disarchive",
    timeout: float = 30.0,
    on_log: Callable[[str], None] | None = None,
) -> str:
    """Run GNU Guix disarchive on an archive and return its specification.

    The specification is an S-expression that describes the archive format
    and metadata. It can be used with ``disarchive assemble`` to recreate
    the exact same archive file from its directory contents.

    A timeout is applied because ``disarchive disassemble`` can hang
    indefinitely on some archives. When the timeout is reached the archive
    is treated as if it could not be disassembled.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".disarchive", delete=False
    ) as spec_file:
        spec_path = spec_file.name

    try:
        cmd = [disarchive_binary, "disassemble", archive_path, "-o", spec_path]
        if on_log:
            on_log(f"capturing disarchive specification for {archive_path}...")
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout)
        spec = Path(spec_path).read_text()
        if not spec.strip():
            raise DisarchiveError(f"disarchive produced an empty spec for {archive_path}")
        return spec
    except subprocess.TimeoutExpired as exc:
        raise DisarchiveTimeoutError(
            f"disarchive disassemble timed out after {timeout}s for {archive_path}"
        ) from exc
    except (subprocess.CalledProcessError, OSError) as exc:
        raise DisarchiveError(f"could not disassemble {archive_path}: {exc}") from exc
    finally:
        _cleanup(spec_path)


def unpack_archive(archive_path: str) -> str:
    """Unpack an archive to a temporary directory and return its path.

    Supports the archive formats handled by Nix's standard unpack phase:
    tarballs (including ``.tar.gz``, ``.tgz``, ``.tar.bz2``, ``.tar.xz``,
    ``.tar.lzma``) and zip files.
    """
    path = Path(archive_path)
    suffixes = [s.lower() for s in path.suffixes]
    lowered_name = path.name.lower()

    if _looks_like_tar(suffixes, lowered_name):
        try:
            return _unpack_tar(archive_path)
        except (tarfile.TarError, OSError):
            pass

    if ".zip" in suffixes or _is_zip(archive_path):
        try:
            return _unpack_zip(archive_path)
        except (zipfile.BadZipFile, OSError):
            pass

    raise DisarchiveError(f"could not unpack {archive_path}")


def _looks_like_tar(suffixes: list[str], lowered_name: str) -> bool:
    if ".tar" in suffixes:
        return True
    if lowered_name.endswith(".tgz"):
        return True
    # A tarball with a compression suffix but no explicit .tar (e.g. .tar.gz).
    if len(suffixes) >= 2 and suffixes[-1] in (".gz", ".bz2", ".xz", ".lzma"):
        return True
    return False


def _unpack_tar(archive_path: str) -> str:
    out_dir = tempfile.mkdtemp(prefix="nix-fod-swh-checker-tar-")
    try:
        with tarfile.open(archive_path, "r:*") as tf:
            try:
                tf.extractall(out_dir, filter="data")
            except TypeError:
                # Python < 3.12 does not support the filter argument.
                tf.extractall(out_dir)
        return out_dir
    except BaseException:
        _cleanup(out_dir)
        raise


def _unpack_zip(archive_path: str) -> str:
    out_dir = tempfile.mkdtemp(prefix="nix-fod-swh-checker-zip-")
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            try:
                zf.extractall(out_dir, filter="data")
            except TypeError:
                # Python < 3.12 does not support the filter argument.
                zf.extractall(out_dir)
        return out_dir
    except BaseException:
        _cleanup(out_dir)
        raise


def _is_zip(archive_path: str) -> bool:
    try:
        with open(archive_path, "rb") as f:
            return f.read(4) == b"PK\x03\x04"
    except OSError:
        return False


def _single_top_level_directory(path: str) -> str | None:
    entries = list(Path(path).iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return str(entries[0])
    return None


def _cleanup(path: str) -> None:
    try:
        shutil.rmtree(path, ignore_errors=True)
    except NotADirectoryError:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
