"""Generate Nix expressions for SWH-backed fixed-output derivations.

For every FOD that is known to Software Heritage, we can build an alternative
FOD that downloads the same content from SWH instead of the original URL.
Because the output hash is fixed to the same value, building these
SWH-backed FODs populates the Nix store with the exact store paths the
original derivation would have produced, allowing a subsequent build of the
original installable to succeed even when upstream sources are unavailable.
"""
from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from typing import Callable

from .models import SWHCheckResult, SWHLookupMethod

_SWH_API_URL = "https://archive.softwareheritage.org/api/1"


class UnsupportedSWHFodError(RuntimeError):
    """Raised when a SWH-backed FOD cannot be generated for a result."""


@dataclass
class SWHFodExpression:
    """A Nix expression that builds a SWH-backed FOD.

    The expression is a function of a single argument `pkgs` (a Nixpkgs
    invocation) and evaluates to a derivation whose output hash matches the
    original FOD.
    """

    label: str
    nix_code: str


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
            hash_algo=fod.hash_algo,
            hash_hex=fod.hash_hex,
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
    return f"""{{ pkgs }}:
pkgs.stdenv.mkDerivation {{
  name = {shlex.quote(name)};
  outputHashMode = "flat";
  outputHashAlgo = {shlex.quote(hash_algo)};
  outputHash = {shlex.quote(hash_hex)};
  buildCommand = ''
    ${{pkgs.curl}}/bin/curl -L -f -o $out {shlex.quote(url)}
  '';
}}
"""


def _directory_fod_derivation(
    *,
    name: str,
    hash_algo: str,
    hash_hex: str,
    swhid: str,
) -> str:
    url = f"{_SWH_API_URL}/vault/flat/{swhid}/raw"
    return f"""{{ pkgs }}:
pkgs.stdenv.mkDerivation {{
  name = {shlex.quote(name)};
  outputHashMode = "recursive";
  outputHashAlgo = {shlex.quote(hash_algo)};
  outputHash = {shlex.quote(hash_hex)};
  nativeBuildInputs = [ pkgs.curl pkgs.gnutar ];
  buildCommand = ''
    mkdir -p tmp
    ${{pkgs.curl}}/bin/curl -L -f -o tmp/bundle.tar.gz {shlex.quote(url)}
    ${{pkgs.gnutar}}/bin/tar -xzf tmp/bundle.tar.gz -C tmp
    dir=$(find tmp -mindepth 1 -maxdepth 1 -type d | head -n1)
    mv "$dir" $out
  '';
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
    escaped_spec = _escape_heredoc(spec)
    return f"""{{ pkgs }}:
pkgs.stdenv.mkDerivation {{
  name = {shlex.quote(name)};
  outputHashMode = "flat";
  outputHashAlgo = {shlex.quote(hash_algo)};
  outputHash = {shlex.quote(hash_hex)};
  nativeBuildInputs = [ pkgs.disarchive pkgs.curl pkgs.gnutar ];
  buildCommand = ''
    mkdir -p tmp
    ${{pkgs.curl}}/bin/curl -L -f -o tmp/bundle.tar.gz {shlex.quote(url)}
    ${{pkgs.gnutar}}/bin/tar -xzf tmp/bundle.tar.gz -C tmp
    dir=$(find tmp -mindepth 1 -maxdepth 1 -type d | head -n1)
    cat > tmp/spec <<'DISARCHIVE_EOF'
{escaped_spec}
DISARCHIVE_EOF
    ${{pkgs.disarchive}}/bin/disarchive assemble "$dir" tmp/spec -o $out
  '';
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
    escaped_spec = _escape_heredoc(spec)
    return f"""{{ pkgs }}:
pkgs.stdenv.mkDerivation {{
  name = {shlex.quote(name)};
  outputHashMode = "flat";
  outputHashAlgo = {shlex.quote(hash_algo)};
  outputHash = {shlex.quote(hash_hex)};
  nativeBuildInputs = [ pkgs.disarchive pkgs.curl pkgs.gnutar ];
  buildCommand = ''
    mkdir -p tmp
    ${{pkgs.curl}}/bin/curl -L -f -o tmp/bundle.tar.gz {shlex.quote(url)}
    ${{pkgs.gnutar}}/bin/tar -xzf tmp/bundle.tar.gz -C tmp
    stripped=$(find tmp -mindepth 1 -maxdepth 1 -type d | head -n1)
    mkdir -p tmp/wrapped/{shlex.quote(top_dir)}
    find "$stripped" -mindepth 1 -maxdepth 1 -exec mv {{}} tmp/wrapped/{shlex.quote(top_dir)}/ \\;
    cat > tmp/spec <<'DISARCHIVE_EOF'
{escaped_spec}
DISARCHIVE_EOF
    ${{pkgs.disarchive}}/bin/disarchive assemble tmp/wrapped tmp/spec -o $out
  '';
}}
"""


def _safe_name(name: str) -> str:
    """Return a Nix-safe derivation name."""
    safe = "".join(c if c.isalnum() or c in "-_+." else "_" for c in name)
    return safe or "swh-backed-fod"


def _escape_heredoc(text: str) -> str:
    """Escape text so it can safely be embedded in a Nix bash heredoc.

    The heredoc uses the quoted delimiter ``DISARCHIVE_EOF``, so the only
    thing that would terminate it prematurely is a line containing exactly
    that delimiter. We defensively indent such lines so they are no longer
    interpreted as the closing delimiter.
    """
    return "\n".join(
        f" {line}" if line == "DISARCHIVE_EOF" else line for line in text.splitlines()
    )


def link_farm_expression(
    expressions: list[SWHFodExpression],
    *,
    name: str = "swh-backed-fods",
) -> str:
    """Return a Nix expression that builds all SWH-backed FODs as a link farm."""
    entries = []
    for expr in expressions:
        safe_name = _safe_name(expr.label)
        entries.append(f'    {{ name = {shlex.quote(safe_name)}; path = ({expr.nix_code}) pkgs; }}')
    entries_str = "\n".join(entries)
    return f"""{{ pkgs ? import <nixpkgs> {{}} }}:
pkgs.linkFarm {shlex.quote(name)} [
{entries_str}
]
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
        f.write(link_farm_expression(expressions))

    if on_log:
        on_log(f"wrote {len(expressions)} SWH-backed FOD(s) to {path}")

    return expressions
