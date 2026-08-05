"""Decide how to check a given FOD against Software Heritage, and run the check.

Nix fixed-output derivations (FODs) can be content-addressed in different
ways (see the `method` field of `nix derivation show`'s output), and only
some of them map directly onto an identifier Software Heritage understands:

- `method="git"`: the hash is computed the same way git computes blob/tree
  hashes, so it is directly comparable to a Software Heritage persistent
  identifier (SWHID) via the `/known/` endpoint.
- `method="flat"`: the hash is a plain checksum (sha1/sha256/...) of the raw
  file bytes, which is exactly what the SWH `/content/{algo}:{hash}/`
  endpoint indexes.
- `method="nar"` (the common case, e.g. `fetchurl`/`fetchzip` outputs that
  are directories): the hash is computed over the NAR serialization of the
  output, which has no equivalent in Software Heritage's data model. There is
  no direct way to check such a hash; the best we can do is look up the
  FOD's source URL(s) as a Software Heritage "origin".
"""
from __future__ import annotations

from .models import FixedOutputDerivation, SWHCheckResult, SWHLookupMethod
from .swh import CONTENT_LOOKUP_ALGOS, SWHClient

_ARCHIVE_URL = "https://archive.softwareheritage.org"


def check_fod(fod: FixedOutputDerivation, client: SWHClient) -> SWHCheckResult:
    """Check a single FOD against Software Heritage, choosing the most
    appropriate comparison strategy for its content-addressing method.
    """
    if fod.method == "git" and fod.hash_algo == "sha1" and fod.hash_hex:
        return _check_via_swhid(fod, client)

    if fod.method == "flat" and fod.hash_algo in CONTENT_LOOKUP_ALGOS and fod.hash_hex:
        return _check_via_content_hash(fod, client)

    if fod.urls:
        return _check_via_origin(fod, client)

    return SWHCheckResult(
        fod=fod,
        known=None,
        method=SWHLookupMethod.UNSUPPORTED,
        detail=(
            f"no direct hash comparison is possible for method={fod.method!r} "
            f"algo={fod.hash_algo!r}, and no source URL is available"
        ),
    )


def _check_via_content_hash(fod: FixedOutputDerivation, client: SWHClient) -> SWHCheckResult:
    result = client.lookup_content(fod.hash_algo, fod.hash_hex)
    return SWHCheckResult(
        fod=fod,
        known=result.known,
        method=SWHLookupMethod.CONTENT_HASH,
        detail=f"content lookup by {fod.hash_algo}:{fod.hash_hex}",
        swh_url=f"{_ARCHIVE_URL}/api/1/content/{fod.hash_algo}:{fod.hash_hex}/",
    )


def _check_via_swhid(fod: FixedOutputDerivation, client: SWHClient) -> SWHCheckResult:
    # We don't know upfront whether the FOD output is a single file (SWH
    # "content") or a directory (SWH "directory"), so probe both.
    candidates = [f"swh:1:cnt:{fod.hash_hex}", f"swh:1:dir:{fod.hash_hex}"]
    known_map = client.lookup_known_swhids(candidates)
    known_swhids = [swhid for swhid, known in known_map.items() if known]
    if known_swhids:
        return SWHCheckResult(
            fod=fod,
            known=True,
            method=SWHLookupMethod.SWHID_KNOWN,
            detail=f"known as {', '.join(known_swhids)}",
            swh_url=f"{_ARCHIVE_URL}/{known_swhids[0]}",
        )
    return SWHCheckResult(
        fod=fod,
        known=False,
        method=SWHLookupMethod.SWHID_KNOWN,
        detail=f"neither {candidates[0]} nor {candidates[1]} are known",
    )


def _check_via_origin(fod: FixedOutputDerivation, client: SWHClient) -> SWHCheckResult:
    for url in fod.urls:
        if client.lookup_origin(url):
            return SWHCheckResult(
                fod=fod,
                known=True,
                method=SWHLookupMethod.ORIGIN_URL,
                detail=f"origin {url} has been archived",
                swh_url=f"{_ARCHIVE_URL}/browse/origin/?origin_url={url}",
            )
    detail = (
        "none of the source URLs are known as archived origins "
        f"({', '.join(fod.urls)}); the FOD's own hash "
        f"(method={fod.method!r}) cannot be compared directly"
    )
    return SWHCheckResult(fod=fod, known=False, method=SWHLookupMethod.ORIGIN_URL, detail=detail)
