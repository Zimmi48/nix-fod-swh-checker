"""Compute Software Heritage persistent identifiers (SWHIDs) for local paths
using the reference `swh identify` command-line tool from the swh-model
project (https://docs.softwareheritage.org/devel/swh-model/cli.html).

Delegating to the actual tool (rather than reimplementing git-compatible
object hashing) guarantees the computed SWHID matches exactly what Software
Heritage itself would compute for the same content.
"""
from __future__ import annotations

import subprocess


class SWHIdentifyError(RuntimeError):
    """Raised when the `swh identify` command fails or returns unparseable output."""


def compute_swhid(path: str, *, swh_binary: str = "swh") -> str:
    """Compute the intrinsic SWHID of a local file or directory.

    Runs `swh identify --no-filename <path>`, which prints a single
    `swh:1:{cnt,dir}:<hash>` identifier derived directly from the object's
    content and, for directories, the content of its entries.
    """
    cmd = [swh_binary, "identify", "--no-filename", path]
    try:
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise SWHIdentifyError(f"could not find the '{swh_binary}' executable") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else ""
        raise SWHIdentifyError(
            f"'{' '.join(cmd)}' failed with exit code {exc.returncode}: {stderr}"
        ) from exc

    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    swhid = lines[0] if lines else ""
    if not swhid.startswith("swh:1:"):
        raise SWHIdentifyError(f"unexpected output from '{' '.join(cmd)}': {proc.stdout!r}")
    return swhid
