"""Helpers for extracting fixed-output derivations (FODs) from Nix.

See https://nix.dev/manual/nix/stable/command-ref/new-cli/nix3-derivation-show
for the JSON format produced by `nix derivation show`.
"""
from __future__ import annotations

import json
import subprocess
from typing import Callable, Iterable, Iterator

from .models import FixedOutputDerivation


class NixCommandError(RuntimeError):
    """Raised when `nix derivation show` fails or returns unparseable output."""


def show_derivations_recursive(
    installable: str,
    *,
    nix_binary: str = "nix",
    extra_args: Iterable[str] | None = None,
    on_log: Callable[[str], None] | None = None,
) -> dict[str, dict]:
    """Run `nix derivation show --recursive <installable>` and parse its JSON output.

    Returns a mapping of `.drv` store paths to derivation objects.
    """
    cmd = [nix_binary, "derivation", "show", "--recursive", *(extra_args or []), installable]
    if on_log:
        on_log(
            f"running 'nix derivation show --recursive {installable}' "
            "(this can take a while for large dependency graphs)..."
        )
    proc = _run_nix(cmd, nix_binary)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise NixCommandError(f"could not parse JSON output of '{' '.join(cmd)}'") from exc


def realise_fod(
    fod: FixedOutputDerivation,
    *,
    nix_binary: str = "nix",
    extra_args: Iterable[str] | None = None,
    on_log: Callable[[str], None] | None = None,
) -> str:
    """Realise a single FOD output and return its resulting store path.

    This runs `nix build --no-link --print-out-paths <drv>^<output>`, which
    fetches the output from any configured substituter (e.g. the NixOS
    binary cache) whenever possible, only falling back to actually
    downloading/building it from scratch if it isn't substitutable.
    """
    installable = f"{fod.drv_path}^{fod.output_name}"
    cmd = [nix_binary, "build", "--no-link", "--print-out-paths", *(extra_args or []), installable]
    if on_log:
        on_log(
            f"realising {fod.label} (fetching from a binary cache or building it, "
            "this can be slow)..."
        )
    out_paths = _run_nix(cmd, nix_binary).stdout.split()
    if not out_paths:
        raise NixCommandError(f"'{' '.join(cmd)}' produced no output path")
    return out_paths[0]


def _run_nix(cmd: list[str], nix_binary: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise NixCommandError(f"could not find the '{nix_binary}' executable") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else ""
        raise NixCommandError(
            f"'{' '.join(cmd)}' failed with exit code {exc.returncode}: {stderr}"
        ) from exc


def iter_fixed_output_derivations(
    derivations: dict[str, dict],
) -> Iterator[FixedOutputDerivation]:
    """Yield every fixed-output derivation output found in `derivations`.

    `derivations` is expected to be in the JSON format produced by
    `nix derivation show`, i.e. a mapping of `.drv` store paths to derivation
    objects, each with an `outputs` mapping of output names to
    `{"path", "method", "hashAlgo", "hash"}` objects. An output is a FOD
    exactly when its `hash` field is set.

    Many Nix versions don't actually populate the `method` field in
    `outputs` and only expose it as the legacy `outputHashMode` environment
    variable (`"flat"` or `"recursive"`), so that's used as a fallback.
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
                method=_output_method(output, env),
                hash_algo=output.get("hashAlgo"),
                hash_hex=hash_hex,
            )


def _output_method(output: dict, env: dict) -> str | None:
    method = output.get("method")
    if method:
        return method
    output_hash_mode = env.get("outputHashMode")
    if output_hash_mode == "recursive":
        return "nar"
    return output_hash_mode or None
