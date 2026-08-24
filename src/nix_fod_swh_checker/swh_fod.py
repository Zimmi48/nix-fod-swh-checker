"""Generate Nix expressions for SWH-backed fixed-output derivations.

For every FOD that is known to Software Heritage, we can build an alternative
expression that downloads the same content from SWH instead of the original
URL.  Because the output hash is fixed to the same value, building these
SWH-backed FODs populates the Nix store with the exact store paths the
original derivation would have produced, allowing a subsequent build of the
original installable to succeed even when upstream sources are unavailable.

Single-file expressions use only Nix builtins.  Directory and disarchive
expressions need a ``pkgs`` argument (defaulting to a pinned Nixpkgs) for
tools such as ``curl``, ``tar`` and ``disarchive``.
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

_ARCHIVE_BUILD_TOOLS = (
    "pkgs.curl",
    "pkgs.cacert",
    "pkgs.gnutar",
    "pkgs.bzip2",
    "pkgs.xz",
)
_DISARCHIVE_BUILD_TOOLS = ("pkgs.disarchive",) + _ARCHIVE_BUILD_TOOLS


@dataclass
class SWHFodExpression:
    """A Nix expression that builds a SWH-backed FOD.

    The expression evaluates to a store path whose contents match the
    original FOD output.  Single-file expressions use only Nix builtins;
    directory and disarchive expressions require ``pkgs`` in scope.
    """

    label: str
    nix_code: str


def vault_swhids_for_results(results: list[SWHCheckResult]) -> set[str]:
    """Return the SWHIDs whose vault flat archives must be cooked for ``results``.

    Vault flat bundles are only needed for directory-backed expressions
    (``swh:1:dir:...``). Single files are fetched via the ``/content/``
    endpoint and do not require pre-cooking. Unknown or undetermined results
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
    """Return a Nix expression for a SWH-backed FOD, or None if undetermined.

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


# Map from the `method` field reported by `nix derivation show` to the
# corresponding Nix `outputHashMode`.  A content SWHID always refers to raw
# file bytes, but the original FOD may have hashed those bytes in different
# ways; the generated derivation must use the same mode or the output hash
# will not match.
_FOD_METHOD_TO_OUTPUT_HASH_MODE = {
    "flat": "flat",
    "nar": "recursive",
    "git": "git",
}


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
            output_hash_mode="flat",
            executable=fod.executable,
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
    output_hash_mode = _FOD_METHOD_TO_OUTPUT_HASH_MODE.get(fod.method or "flat")
    if output_hash_mode is None:
        return None
    sha1_git = result.swhid.removeprefix("swh:1:cnt:")
    url = f"{_SWH_API_URL}/content/sha1_git:{sha1_git}/raw/"
    hash_algo = fod.hash_algo or _algo_from_hash(fod.hash_hex) or "sha1"
    hash_hex = fod.hash_hex or sha1_git
    # Recursive/NAR-hashed single-file FODs need the executable bit set so
    # that ``builtin:fetchurl`` computes the NAR hash of an executable file
    # rather than falling back to a flat content hash.
    executable = output_hash_mode == "recursive" or fod.executable
    return SWHFodExpression(
        label=fod.label,
        nix_code=_flat_fod_derivation(
            name=_safe_name(fod.name),
            hash_algo=hash_algo,
            hash_hex=hash_hex,
            url=url,
            output_hash_mode=output_hash_mode,
            executable=executable,
        ),
    )


def _directory_swhid_expression(result: SWHCheckResult) -> SWHFodExpression | None:
    fod = result.fod
    hash_algo = fod.hash_algo or _algo_from_hash(fod.hash_hex)
    if not hash_algo or not fod.hash_hex:
        return None
    return SWHFodExpression(
        label=fod.label,
        nix_code=_directory_fod_derivation(
            name=_safe_name(fod.name),
            hash_algo=hash_algo,
            hash_hex=fod.hash_hex,
            swhid=result.swhid,
        ),
    )


def _disarchive_expression(result: SWHCheckResult) -> SWHFodExpression | None:
    fod = result.fod
    hash_algo = fod.hash_algo or _algo_from_hash(fod.hash_hex)
    if not hash_algo or not fod.hash_hex:
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
                hash_algo=hash_algo,
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
                hash_algo=hash_algo,
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
    output_hash_mode: str,
    executable: bool = False,
) -> str:
    lines = [
        f"  name = {nix_quote(name)};",
        "  system = builtins.currentSystem;",
        '  builder = "builtin:fetchurl";',
        f"  outputHashMode = {nix_quote(output_hash_mode)};",
        f"  outputHashAlgo = {nix_quote(hash_algo)};",
        f"  outputHash = {nix_quote(hash_hex)};",
    ]
    if executable:
        lines.append('  executable = "1";')
    lines.append(f"  url = {nix_quote(url)};")
    body = "\n".join(lines)
    return f"""builtins.derivation {{
{body}
}}
"""


def _archive_native_build_inputs(*, include_disarchive: bool) -> str:
    tools = _DISARCHIVE_BUILD_TOOLS if include_disarchive else _ARCHIVE_BUILD_TOOLS
    return "[ " + " ".join(tools) + " ]"


def cache_warmer_derivation() -> str:
    """Return the checked-in cache warmer derivation code."""
    return f"""{{ pkgs }}:
pkgs.stdenv.mkDerivation {{
  name = "cache-warmer";
  nativeBuildInputs = {_archive_native_build_inputs(include_disarchive=True)};
  buildCommand = "mkdir -p $out";
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
    return f"""pkgs.stdenv.mkDerivation {{
  name = {nix_quote(name)};
  outputHashMode = "recursive";
  outputHashAlgo = {nix_quote(hash_algo)};
  outputHash = {nix_quote(hash_hex)};
      nativeBuildInputs = {_archive_native_build_inputs(include_disarchive=False)};
  buildCommand = ''
    export SSL_CERT_FILE="${{pkgs.cacert}}/etc/ssl/certs/ca-bundle.crt"
    mkdir -p tmp
    curl -L -f -o tmp/bundle {nix_quote(url)}
    case $(file -b --mime-type tmp/bundle) in
      application/x-bzip2) tar -xjf tmp/bundle -C tmp ;;
      application/x-xz) tar -xJf tmp/bundle -C tmp ;;
      application/gzip) tar -xzf tmp/bundle -C tmp ;;
      application/x-tar) tar -xf tmp/bundle -C tmp ;;
      *) tar -xaf tmp/bundle -C tmp ;;
    esac
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
    return f"""let
  specFile = builtins.toFile "disarchive.spec" {nix_quote(spec)};
in
pkgs.stdenv.mkDerivation {{
  name = {nix_quote(name)};
  outputHashMode = "flat";
  outputHashAlgo = {nix_quote(hash_algo)};
  outputHash = {nix_quote(hash_hex)};
  nativeBuildInputs = {_archive_native_build_inputs(include_disarchive=True)};
  buildCommand = ''
    export SSL_CERT_FILE="${{pkgs.cacert}}/etc/ssl/certs/ca-bundle.crt"
    mkdir -p tmp
    curl -L -f -o tmp/bundle {nix_quote(url)}
    case $(file -b --mime-type tmp/bundle) in
      application/x-bzip2) tar -xjf tmp/bundle -C tmp ;;
      application/x-xz) tar -xJf tmp/bundle -C tmp ;;
      application/gzip) tar -xzf tmp/bundle -C tmp ;;
      application/x-tar) tar -xf tmp/bundle -C tmp ;;
      *) tar -xaf tmp/bundle -C tmp ;;
    esac
    dir=$(find tmp -mindepth 1 -maxdepth 1 -type d | head -n1)
    disarchive assemble "$dir" ${{specFile}} -o $out
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
    return f"""let
  specFile = builtins.toFile "disarchive.spec" {nix_quote(spec)};
in
pkgs.stdenv.mkDerivation {{
  name = {nix_quote(name)};
  outputHashMode = "flat";
  outputHashAlgo = {nix_quote(hash_algo)};
  outputHash = {nix_quote(hash_hex)};
  nativeBuildInputs = {_archive_native_build_inputs(include_disarchive=True)};
  topDir = {nix_quote(top_dir)};
  buildCommand = ''
    export SSL_CERT_FILE="${{pkgs.cacert}}/etc/ssl/certs/ca-bundle.crt"
    mkdir -p tmp
    curl -L -f -o tmp/bundle {nix_quote(url)}
    case $(file -b --mime-type tmp/bundle) in
      application/x-bzip2) tar -xjf tmp/bundle -C tmp ;;
      application/x-xz) tar -xJf tmp/bundle -C tmp ;;
      application/gzip) tar -xzf tmp/bundle -C tmp ;;
      application/x-tar) tar -xf tmp/bundle -C tmp ;;
      *) tar -xaf tmp/bundle -C tmp ;;
    esac
    stripped=$(find tmp -mindepth 1 -maxdepth 1 -type d | head -n1)
    mkdir -p "tmp/wrapped/$topDir"
    find "$stripped" -mindepth 1 -maxdepth 1 -exec mv {{}} "tmp/wrapped/$topDir/" \\;
    disarchive assemble tmp/wrapped ${{specFile}} -o $out
  '';
}}
"""


def _algo_from_hash(hash_value: str | None) -> str | None:
    """Return the algorithm prefix of an SRI hash, if any.

    Nix SRI hashes have the form ``<algo>-<base64>``. Newer ``nix derivation
    show`` does not populate the per-output ``hashAlgo`` field, so we infer
    the algorithm directly from the hash string when needed.
    """
    if not hash_value:
        return None
    if ":" in hash_value:
        return None
    if "-" not in hash_value:
        return None
    algo, _, rest = hash_value.partition("-")
    if not algo or not rest:
        return None
    return algo


def _safe_name(name: str) -> str:
    """Return a Nix-safe derivation name."""
    safe = "".join(c if c.isalnum() or c in "-_+." else "_" for c in name)
    return safe or "swh-backed-fod"


def _safe_attr_name(name: str) -> str:
    """Return a Nix-safe attribute name from a user-facing label.

    Attribute names may not contain `.` or `-` and must not start with a digit,
    so this function replaces unsafe characters with underscores, collapses
    consecutive underscores, and prefixes a leading digit (or an entirely
    numeric name) with ``attr_``.
    """
    safe = "".join(c if c.isalnum() or c == "_" else "_" for c in name)
    # Collapse consecutive underscores so the transformed label is shorter and
    # deterministic regardless of how many non-alphanumeric characters were
    # adjacent in the original label.
    import re

    safe = re.sub(r"_+", "_", safe)
    if not safe:
        return "swh_backed_fod"
    safe = safe.strip("_")
    if not safe:
        return "swh_backed_fod"
    if safe[0].isdigit() or safe.isdigit():
        safe = "attr_" + safe
    return safe


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
    to each SWH-backed expression.  Flat files use only Nix builtins;
    directory and disarchive expressions need ``pkgs`` for tools such as
    ``curl``, ``tar`` and ``disarchive``.  ``pkgs`` defaults to a pinned
    Nixpkgs fetched with ``builtins.fetchTarball`` so the file evaluates
    standalone, but callers can override it with their own Nixpkgs invocation.

    The ``name`` parameter is kept for backward compatibility but is ignored
    because the returned attribute set is not wrapped in a single derivation.
    """
    del name  # kept for backward compatibility
    entries = []
    seen: set[str] = set()
    for expr in expressions:
        attr_name = _safe_attr_name(expr.label)
        # Labels can collide once sanitized (e.g. "a.b" and "a_b"). Keep the
        # first occurrence and suffix duplicates with a counter.
        if attr_name in seen:
            counter = 1
            while f"{attr_name}_{counter}" in seen:
                counter += 1
            attr_name = f"{attr_name}_{counter}"
        seen.add(attr_name)
        entries.append(f"  {nix_quote(attr_name)} = {expr.nix_code.rstrip()};")
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

    The tool aims to cover every known FOD.  When a known result cannot be
    turned into a SWH-backed expression, a warning is emitted so the gap is
    visible rather than silently ignored.

    Returns the list of expressions that were written.
    """
    expressions: list[SWHFodExpression] = []
    for result in results:
        if not result.known:
            continue
        expr = swh_fod_expression(result)
        if expr is not None:
            expressions.append(expr)
        elif on_log:
            on_log(
                f"warning: {result.fod.label} is known to Software Heritage "
                f"(method={result.method.value}) but cannot be turned into a "
                "SWH-backed FOD expression"
            )

    with open(path, "w") as f:
        f.write(swh_fods_expression(expressions))

    if on_log:
        on_log(f"wrote {len(expressions)} SWH-backed FOD(s) to {path}")

    return expressions
