"""Decompress a realised FOD output and check its directory SWHID.

When a FOD output is a single file (``method="flat"`` or ``method="git"`` as
a blob) that is not known to Software Heritage as a content object, it may
still be an archive whose *contents* are archived as a directory. This module
realises such FODs, unpacks the resulting archive with standard tools, and
looks up the ``swh:1:dir:`` identifier of the unpacked tree.

It also uses GNU Guix ``disarchive`` to capture the archive's metadata
specification. ``disarchive`` computes its own directory SWHID, which may
include a single top-level directory that Nix normally strips. We therefore
keep track of two SWHIDs:

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

from .models import FixedOutputDerivation, SWHCheckResult, SWHLookupMethod
from .nix import NixCommandError, realise_fod
from .swh import SWHClient
from .swhid import SWHIdentifyError, compute_swhid

_ARCHIVE_URL = "https://archive.softwareheritage.org"


class DisarchiveError(RuntimeError):
    """Raised when an archive cannot be decompressed or disassembled."""


def try_disarchive(
    fod: FixedOutputDerivation,
    client: SWHClient,
    *,
    nix_binary: str = "nix",
    swh_binary: str = "swh",
    disarchive_binary: str = "disarchive",
    on_log: Callable[[str], None] | None = None,
) -> SWHCheckResult | None:
    """Realise a FOD, try to unpack it, and check its directory SWHID.

    Returns a :class:`SWHCheckResult` when the archive was successfully
    unpacked and its directory SWHID looked up, or ``None`` when the FOD could
    not be realised, is not an archive, or could not be unpacked.
    """
    try:
        archive_path = realise_fod(fod, nix_binary=nix_binary, on_log=on_log)
    except NixCommandError:
        return None

    if not Path(archive_path).is_file():
        return None

    try:
        spec = disassemble_archive(archive_path, disarchive_binary=disarchive_binary)
    except DisarchiveError:
        spec = None

    disarchive_swhid = _extract_disarchive_swhid(spec) if spec else None

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
            content_path, swh_binary=swh_binary, on_log=on_log
        )
    except SWHIdentifyError:
        _cleanup(unpacked_path)
        return None

    candidates = [swhid for swhid in (stripped_swhid, disarchive_swhid) if swhid]
    known_map = client.lookup_known_swhids(candidates)
    stripped_known = known_map.get(stripped_swhid, False)
    disarchive_known = known_map.get(disarchive_swhid, False)
    known = stripped_known or disarchive_known

    _cleanup(unpacked_path)

    # Prefer the disarchive SWHID for reporting when it is known, because that
    # is the directory disarchive can rebuild from directly. Otherwise report
    # the stripped SWHID, which is more likely to be archived.
    reported_swhid = disarchive_swhid if disarchive_known else stripped_swhid

    return SWHCheckResult(
        fod=fod,
        known=known,
        method=SWHLookupMethod.KNOWN_AFTER_DISARCHIVE,
        detail=f"unpacked {archive_path} and computed {stripped_swhid}",
        swhid=reported_swhid,
        swh_url=f"{_ARCHIVE_URL}/{reported_swhid}" if known else None,
        disarchive_spec=spec,
        disarchive_swhid=disarchive_swhid,
        disarchive_top_dir=stripped_top_dir,
    )


def _extract_disarchive_swhid(spec: str) -> str | None:
    """Return the ``swh:1:dir:`` identifier embedded in a disarchive spec.

    The specification contains a single ``directory-ref`` with a list of
    addresses; we extract the SWHID address if present.
    """
    match = re.search(r'\(swhid\s+"(swh:1:dir:[a-f0-9]+)"\)', spec)
    return match.group(1) if match else None


def disassemble_archive(
    archive_path: str,
    *,
    disarchive_binary: str = "disarchive",
) -> str:
    """Run GNU Guix disarchive on an archive and return its specification.

    The specification is an S-expression that describes the archive format
    and metadata. It can be used with ``disarchive assemble`` to recreate
    the exact same archive file from its directory contents.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".disarchive", delete=False
    ) as spec_file:
        spec_path = spec_file.name

    try:
        cmd = [disarchive_binary, "disassemble", archive_path, "-o", spec_path]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        spec = Path(spec_path).read_text()
        if not spec.strip():
            raise DisarchiveError(f"disarchive produced an empty spec for {archive_path}")
        return spec
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
