"""Helpers for extracting fixed-output derivations (FODs) from Nix.

See https://nix.dev/manual/nix/stable/command-ref/new-cli/nix3-derivation-show
for the JSON format produced by `nix derivation show`.
"""
from __future__ import annotations

import json
import subprocess
from typing import Iterable, Iterator

from .models import FixedOutputDerivation


class NixCommandError(RuntimeError):
    """Raised when `nix derivation show` fails or returns unparseable output."""


def show_derivations_recursive(
    installable: str,
    *,
    nix_binary: str = "nix",
    extra_args: Iterable[str] | None = None,
) -> dict[str, dict]:
    """Run `nix derivation show --recursive <installable>` and parse its JSON output.

    Returns a mapping of `.drv` store paths to derivation objects.
    """
    cmd = [nix_binary, "derivation", "show", "--recursive", *(extra_args or []), installable]
    try:
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise NixCommandError(f"could not find the '{nix_binary}' executable") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else ""
        raise NixCommandError(
            f"'{' '.join(cmd)}' failed with exit code {exc.returncode}: {stderr}"
        ) from exc

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise NixCommandError(f"could not parse JSON output of '{' '.join(cmd)}'") from exc


def _extract_urls(env: dict) -> list[str]:
    """Best-effort extraction of source URL(s) from a derivation's environment.

    Most fetchers (`fetchurl`, `fetchzip`, `fetchpatch`, ...) expose the
    download URL(s) as a whitespace-separated `urls` (or singular `url`)
    environment variable.
    """
    for key in ("urls", "url"):
        value = env.get(key)
        if value:
            return value.split()
    return []


def iter_fixed_output_derivations(
    derivations: dict[str, dict],
) -> Iterator[FixedOutputDerivation]:
    """Yield every fixed-output derivation output found in `derivations`.

    `derivations` is expected to be in the JSON format produced by
    `nix derivation show`, i.e. a mapping of `.drv` store paths to derivation
    objects, each with an `outputs` mapping of output names to
    `{"path", "method", "hashAlgo", "hash"}` objects. An output is a FOD
    exactly when its `hash` field is set.
    """
    for drv_path, drv in derivations.items():
        outputs = drv.get("outputs", {}) or {}
        env = drv.get("env", {}) or {}
        for output_name, output in outputs.items():
            hash_hex = output.get("hash")
            if not hash_hex:
                continue
            yield FixedOutputDerivation(
                drv_path=drv_path,
                output_name=output_name,
                output_path=output.get("path"),
                name=drv.get("name", drv_path),
                method=output.get("method"),
                hash_algo=output.get("hashAlgo"),
                hash_hex=hash_hex,
                urls=_extract_urls(env),
            )
