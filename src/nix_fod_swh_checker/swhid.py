"""Compute Software Heritage persistent identifiers (SWHIDs) for local paths
using the reference `swh identify` command-line tool from the swh-model
project (https://docs.softwareheritage.org/devel/swh-model/cli.html).

Delegating to the actual tool (rather than reimplementing git-compatible
object hashing) guarantees the computed SWHID matches exactly what Software
Heritage itself would compute for the same content.
"""
from __future__ import annotations

import subprocess
from typing import Callable

from .cache import Cache


class SWHIdentifyError(RuntimeError):
    """Raised when the `swh identify` command fails or returns unparseable output."""


def compute_swhid(
    path: str,
    *,
    swh_binary: str = "swh",
    on_log: Callable[[str], None] | None = None,
    timeout: float = 30.0,
    cache: Cache | None = None,
    cache_key: str | None = None,
) -> str:
    """Compute the intrinsic SWHID of a local file or directory.

    Runs `swh identify --no-filename <path>`, which prints a single
    `swh:1:{cnt,dir}:<hash>` identifier derived directly from the object's
    content and, for directories, the content of its entries.

    A timeout is applied because `swh identify` can hang indefinitely on
    certain paths (e.g. special files such as FIFOs inside a directory tree).
    When the timeout is reached the FOD is reported as undetermined rather
    than blocking the whole check run.
    """
    cmd = [swh_binary, "identify", "--no-filename", path]

    full_cache_key = f"tool:swh_identify:{cache_key}" if cache_key else None
    if cache is not None and full_cache_key is not None:
        cached = cache.get_tool_result(full_cache_key, timeout)
        if cached is not None:
            if cached.get("timed_out"):
                raise SWHIdentifyError(
                    f"'{' '.join(cmd)}' timed out after {cached['timeout']}s"
                )
            return cached["swhid"]
    if on_log:
        on_log(f"computing the SWHID of {path} via 'swh identify' (may be slow for large trees)...")
    try:
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise SWHIdentifyError(f"could not find the '{swh_binary}' executable") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else ""
        raise SWHIdentifyError(
            f"'{' '.join(cmd)}' failed with exit code {exc.returncode}: {stderr}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        if cache is not None and full_cache_key is not None:
            cache.set(
                full_cache_key,
                {"timed_out": True, "timeout": timeout},
                is_miss=True,
            )
        raise SWHIdentifyError(
            f"'{' '.join(cmd)}' timed out after {timeout}s"
        ) from exc

    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    swhid = lines[0] if lines else ""
    if not swhid.startswith("swh:1:"):
        raise SWHIdentifyError(f"unexpected output from '{' '.join(cmd)}': {proc.stdout!r}")

    if cache is not None and full_cache_key is not None:
        cache.set(full_cache_key, {"swhid": swhid, "timeout": timeout}, is_miss=False)

    return swhid
