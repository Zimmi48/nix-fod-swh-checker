"""Generate Nix expressions for SWH-backed fixed-output derivations.

For every FOD that is known to Software Heritage, we can build an alternative
expression that downloads the same content from SWH instead of the original
URL.  Because the output hash is fixed to the same value, building these
SWH-backed FODs populates the Nix store with the exact store paths the
original derivation would have produced, allowing a subsequent build of the
original installable to succeed even when upstream sources are unavailable.

The generated expressions use only Nix builtins (``builtins.fetchurl``,
``builtins.fetchTarball`` and ``builtins.derivation``) so they do not depend
on any particular Nixpkgs version.  Disarchive reconstructions still need the
``disarchive`` binary available on the builder's ``PATH``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .models import SWHCheckResult, SWHLookupMethod

_SWH_API_URL = "https://archive.softwareheritage.org/api/1"

# Default Nixpkgs used for the disarchive binary in generated expressions.
# This matches the nixpkgs input pinned in flake.lock.
_DEFAULT_NIXPKGS_URL = "https://github.com/NixOS/nixpkgs/archive/e72e4f299401a3689d4b3d5fc6496b11db7064eb.tar.gz"
_DEFAULT_NIXPKGS_SHA256 = "sha256-8fsyqeO+mJqvIzeO4xIpgJe/f7MTbbVTEC6RT6WSXNs="


class UnsupportedSWHFodError(RuntimeError):
    """Raised when a SWH-backed FOD cannot be generated for a result."""


@dataclass
class SWHFodExpression:
    """A Nix expression that builds a SWH-backed FOD.

    The expression evaluates to a store path whose contents match the
    original FOD output.  It uses only Nix builtins and does not take a
    ``pkgs`` argument.
    """

    label: str
    nix_code: str


def vault_swhids_for_results(results: list[SWHCheckResult]) -> set[str]:
    """Return the SWHIDs whose vault flat archives must be cooked for ``results``.

    Vault flat bundles are only needed for directory-backed expressions
    (``swh:1:dir:...``). Single files are fetched via the ``/content/``
    endpoint and do not require pre-cooking. Unknown or unsupported results
    are ignored.
    """
    vault_swhids = set()
    for result in results:
        if not result.known or not result.swhid:
            continue
        expr = swh_fod_expression(result)
        if expr is None or "/vault/flat/" not in expr.nix_code:
            continue
        if result.swhid.startswith("swh:1:dir:"):
            vault_swhids.add(result.swhid)
    return vault_swhids


def swh_fod_expression(result: SWHCheckResult) -> SWHFodExpression | None:
    """Return a Nix expression for a SWH-backed FOD, or None if unsupported.

    Supported cases:

    - ``CONTENT_HASH``: the FOD is a single file whose raw bytes are indexed
      by SWH. We download them via the ``/content/`` endpoint.
    - ``SWHID_KNOWN`` as ``swh:1:cnt:...``: same as above, using the
      ``sha1_git`` identifier.
    - ``SWHID_KNOWN`` as ``swh:1:dir:...``: the FOD is a directory known to
      SWH. We fetch it via the SWH vault flat bundle.
    - ``BUILD_AND_IDENTIFY`` as ``swh:1:dir:...``: same as above; the
      directory contents are identical, so the NAR hash matches.
    - ``KNOWN_AFTER_DISARCHIVE``: the FOD is an archive file whose contents
      are known to SWH as a directory. Reconstructing the exact archive
      requires the GNU Guix ``disarchive`` tool and is handled separately.
    """
    if not result.known or not result.swhid:
        return None

    if result.method == SWHLookupMethod.CONTENT_HASH:
        return _content_hash_expression(result)

    if result.method in (
        SWHLookupMethod.SWHID_KNOWN,
        SWHLookupMethod.BUILD_AND_IDENTIFY,
    ):
        return _swhid_expression(result)

    if result.method == SWHLookupMethod.KNOWN_AFTER_DISARCHIVE:
        return _disarchive_expression(result)

    return None


def _content_hash_expression(result: SWHCheckResult) -> SWHFodExpression | None:
    fod = result.fod
    if not fod.hash_algo or not fod.hash_hex:
        return None
    url = f"{_SWH_API_URL}/content/{fod.hash_algo}:{fod.hash_hex}/raw/"
    return SWHFodExpression(
        label=fod.label,
        nix_code=_flat_fod_derivation(
            name=_safe_name(fod.name),
            hash_algo=fod.hash_algo,
            hash_hex=fod.hash_hex,
            url=url,
        ),
    )


def _swhid_expression(result: SWHCheckResult) -> SWHFodExpression | None:
    if not result.swhid:
        return None

    if result.swhid.startswith("swh:1:cnt:"):
        return _content_swhid_expression(result)

    if result.swhid.startswith("swh:1:dir:"):
        return _directory_swhid_expression(result)

    return None


def _content_swhid_expression(result: SWHCheckResult) -> SWHFodExpression | None:
    fod = result.fod
    sha1_git = result.swhid.removeprefix("swh:1:cnt:")
    url = f"{_SWH_API_URL}/content/sha1_git:{sha1_git}/raw/"
    hash_algo = fod.hash_algo or "sha1"
    hash_hex = fod.hash_hex or sha1_git
    return SWHFodExpression(
        label=fod.label,
        nix_code=_flat_fod_derivation(
            name=_safe_name(fod.name),
            hash_algo=hash_algo,
            hash_hex=hash_hex,
            url=url,
        ),
    )


def _directory_swhid_expression(result: SWHCheckResult) -> SWHFodExpression | None:
    fod = result.fod
    if not fod.hash_algo or not fod.hash_hex:
        return None
    return SWHFodExpression(
        label=fod.label,
        nix_code=_directory_fod_derivation(
            name=_safe_name(fod.name),
            swhid=result.swhid,
        ),
    )


def _disarchive_expression(result: SWHCheckResult) -> SWHFodExpression | None:
    fod = result.fod
    if not fod.hash_algo or not fod.hash_hex:
        return None
    if not result.disarchive_spec:
        return None

    # If disarchive's own SWHID is known, we can rebuild the archive directly
    # from the directory disarchive expects. Otherwise, fall back to the
    # stripped SWHID and re-wrap the stripped contents inside the original
    # top-level directory.
    if result.disarchive_swhid and result.swhid == result.disarchive_swhid:
        return SWHFodExpression(
            label=fod.label,
            nix_code=_disarchive_fod_derivation(
                name=_safe_name(fod.name),
                hash_algo=fod.hash_algo,
                hash_hex=fod.hash_hex,
                swhid=result.disarchive_swhid,
                spec=result.disarchive_spec,
            ),
        )

    if (
        result.swhid
        and result.swhid.startswith("swh:1:dir:")
        and result.disarchive_top_dir
    ):
        return SWHFodExpression(
            label=fod.label,
            nix_code=_disarchive_wrapped_fod_derivation(
                name=_safe_name(fod.name),
                hash_algo=fod.hash_algo,
                hash_hex=fod.hash_hex,
                stripped_swhid=result.swhid,
                top_dir=result.disarchive_top_dir,
                spec=result.disarchive_spec,
            ),
        )

    return None


def _flat_fod_derivation(
    *,
    name: str,
    hash_algo: str,
    hash_hex: str,
    url: str,
) -> str:
    return f"""builtins.derivation {{
  name = {nix_quote(name)};
  system = builtins.currentSystem;
  builder = "builtin:fetchurl";
  outputHashMode = "flat";
  outputHashAlgo = {nix_quote(hash_algo)};
  outputHash = {nix_quote(hash_hex)};
  url = {nix_quote(url)};
}}
"""


def _directory_fod_derivation(
    *,
    name: str,
    swhid: str,
) -> str:
    url = f"{_SWH_API_URL}/vault/flat/{swhid}/raw"
    return f"""builtins.fetchTarball {{
  url = {nix_quote(url)};
  name = {nix_quote(name)};
}}
"""


def _disarchive_fod_derivation(
    *,
    name: str,
    hash_algo: str,
    hash_hex: str,
    swhid: str,
    spec: str,
) -> str:
    url = f"{_SWH_API_URL}/vault/flat/{swhid}/raw"
    return f"""let
  dir = builtins.fetchTarball {{
    url = {nix_quote(url)};
    name = {nix_quote(name)};
  }};
  specFile = builtins.toFile "disarchive.spec" {nix_quote(spec)};
in
builtins.derivation {{
  name = {nix_quote(name)};
  system = builtins.currentSystem;
  builder = "${{pkgs.disarchive}}/bin/disarchive";
  args = [ "assemble" "$dir" specFile "-o" "$out" ];
  inherit specFile;
  outputHashMode = "flat";
  outputHashAlgo = {nix_quote(hash_algo)};
  outputHash = {nix_quote(hash_hex)};
}}
"""


def _disarchive_wrapped_fod_derivation(
    *,
    name: str,
    hash_algo: str,
    hash_hex: str,
    stripped_swhid: str,
    top_dir: str,
    spec: str,
) -> str:
    url = f"{_SWH_API_URL}/vault/flat/{stripped_swhid}/raw"
    return f"""let
  stripped = builtins.fetchTarball {{
    url = {nix_quote(url)};
    name = {nix_quote(name + "-stripped")};
  }};
  specFile = builtins.toFile "disarchive.spec" {nix_quote(spec)};
in
builtins.derivation {{
  name = {nix_quote(name)};
  system = builtins.currentSystem;
  builder = "${{pkgs.disarchive}}/bin/disarchive";
  args = [ "-c" ''
    mkdir -p "tmp/wrapped/$topDir"
    find "${{stripped}}" -mindepth 1 -maxdepth 1 -exec mv {{}} "tmp/wrapped/$topDir/" \\;
    ${{pkgs.disarchive}}/bin/disarchive assemble tmp/wrapped ${{specFile}} -o $out
  '' ];
  inherit specFile;
  topDir = {nix_quote(top_dir)};
  outputHashMode = "flat";
  outputHashAlgo = {nix_quote(hash_algo)};
  outputHash = {nix_quote(hash_hex)};
}}
"""


def _safe_name(name: str) -> str:
    """Return a Nix-safe derivation name."""
    safe = "".join(c if c.isalnum() or c in "-_+." else "_" for c in name)
    return safe or "swh-backed-fod"


def nix_quote(s: str) -> str:
    """Return a Nix double-quoted string literal for ``s``."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("${", "\\${") + '"'


def swh_fods_expression(
    expressions: list[SWHFodExpression],
    *,
    name: str = "swh-backed-fods",
) -> str:
    """Return a Nix expression that builds all SWH-backed FODs.

    The result is a function ``{ pkgs ? ... }: { ... }`` mapping a safe name
    to each SWH-backed expression.  Flat files and directories use only Nix
    builtins; disarchive reconstructions need ``pkgs.disarchive`` as a real
    derivation input.  ``pkgs`` defaults to a pinned Nixpkgs fetched with
    ``builtins.fetchTarball`` so the file evaluates standalone, but callers
    can override it with their own Nixpkgs invocation.

    The ``name`` parameter is kept for backward compatibility but is ignored
    because the returned attribute set is not wrapped in a single derivation.
    """
    del name  # kept for backward compatibility
    entries = []
    for expr in expressions:
        safe_name = _safe_name(expr.label)
        entries.append(f"  {nix_quote(safe_name)} = {expr.nix_code.rstrip()};")
    entries_str = "\n".join(entries)
    return f"""{{ pkgs ? import (builtins.fetchTarball {{
  url = {nix_quote(_DEFAULT_NIXPKGS_URL)};
  sha256 = {nix_quote(_DEFAULT_NIXPKGS_SHA256)};
}}) {{}} }}:
{{
{entries_str}
}}
"""


def write_swh_fods_nix(
    path: str,
    results: list[SWHCheckResult],
    *,
    on_log: Callable[[str], None] | None = None,
) -> list[SWHFodExpression]:
    """Write a Nix expression with SWH-backed FODs for all known results.

    Returns the list of expressions that were written.
    """
    expressions: list[SWHFodExpression] = []
    for result in results:
        expr = swh_fod_expression(result)
        if expr is not None:
            expressions.append(expr)

    with open(path, "w") as f:
        f.write(swh_fods_expression(expressions))

    if on_log:
        on_log(f"wrote {len(expressions)} SWH-backed FOD(s) to {path}")

    return expressions
