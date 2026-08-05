"""Persist in-progress check results to disk.

Checking every FOD reachable from a large attribute (e.g. a full package's
dependency graph) can take a very long time -- realising `nar`-hashed FODs
that aren't already built locally can require downloads, and the Software
Heritage API is rate-limited. This module lets the CLI save its results as it
goes, so an interrupted run doesn't lose already-computed answers and a
subsequent run can pick up where it left off.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from .models import FixedOutputDerivation, SWHCheckResult, SWHLookupMethod


def default_checkpoint_path(installable: str) -> Path:
    """Compute a stable, per-installable checkpoint file path under the
    user's cache directory ($XDG_CACHE_HOME, or ~/.cache as a fallback).
    """
    cache_home = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    digest = hashlib.sha256(installable.encode()).hexdigest()[:16]
    return Path(cache_home) / "nix-fod-swh-checker" / f"{digest}.json"


def load_checkpoint(path: Path) -> dict[str, SWHCheckResult]:
    """Load previously checkpointed results, keyed by FOD label.

    Returns an empty dict if the file doesn't exist or can't be parsed.
    """
    try:
        raw = json.loads(path.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}

    results: dict[str, SWHCheckResult] = {}
    for label, entry in raw.get("results", {}).items():
        try:
            fod = FixedOutputDerivation(**entry["fod"])
            results[label] = SWHCheckResult(
                fod=fod,
                known=entry["known"],
                method=SWHLookupMethod(entry["method"]),
                detail=entry["detail"],
                swhid=entry.get("swhid"),
                swh_url=entry.get("swh_url"),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return results


def save_checkpoint(path: Path, installable: str, results: dict[str, SWHCheckResult]) -> None:
    """Atomically write out the current results, keyed by FOD label."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "installable": installable,
        "results": {
            label: {
                "fod": asdict(result.fod),
                "known": result.known,
                "method": result.method.value,
                "detail": result.detail,
                "swhid": result.swhid,
                "swh_url": result.swh_url,
            }
            for label, result in results.items()
        },
    }

    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-checkpoint-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
