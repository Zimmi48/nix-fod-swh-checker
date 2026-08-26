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
- Anything else (most commonly `method="nar"`, used by `fetchurl`/`fetchzip`
  outputs that are directories): the hash has no equivalent in Software
  Heritage's data model, so there is no way to compare it directly. Instead,
  the FOD is actually realised with `nix build` -- which fetches it from a
  binary cache such as cache.nixos.org whenever possible, rather than
  rebuilding it from scratch -- and its real SWHID is computed from the
  resulting files on disk with the reference `swh identify` tool. That exact
  SWHID is then looked up via the `/known/` endpoint. No guessing is
  involved: the SWHID is derived from the actual archived content.
"""
from __future__ import annotations

from typing import Callable

from .cache import Cache
from .disarchive import _DISARCHIVE_DB_URL, try_disarchive
from .models import FixedOutputDerivation, SWHCheckResult, SWHLookupMethod
from .nix import NixCommandError, realise_fod
from .swh import CONTENT_LOOKUP_ALGOS, SWHClient
from .swhid import SWHIdentifyError, compute_swhid

_ARCHIVE_URL = "https://archive.softwareheritage.org"


def check_fod(
    fod: FixedOutputDerivation,
    client: SWHClient,
    *,
    nix_binary: str = "nix",
    swh_binary: str = "swh",
    swh_identify_timeout: float = 30.0,
    disarchive_timeout: float = 30.0,
    disarchive_db_url: str = _DISARCHIVE_DB_URL,
    skip_disarchive: bool = False,
    cache: Cache | None = None,
    on_log: Callable[[str], None] | None = None,
) -> SWHCheckResult:
    """Check a single FOD against Software Heritage, choosing the most
    appropriate comparison strategy for its content-addressing method.
    """
    if fod.method == "git" and fod.hash_algo == "sha1" and fod.hash_hex:
        if on_log:
            on_log(f"{fod.label}: method=git, checking its SWHID directly")
        return _check_via_swhid(
            fod,
            client,
            nix_binary=nix_binary,
            swh_binary=swh_binary,
            swh_identify_timeout=swh_identify_timeout,
            disarchive_timeout=disarchive_timeout,
            disarchive_db_url=disarchive_db_url,
            skip_disarchive=skip_disarchive,
            cache=cache,
            on_log=on_log,
        )

    if fod.method == "flat" and fod.hash_algo in CONTENT_LOOKUP_ALGOS and fod.hash_hex:
        if on_log:
            on_log(f"{fod.label}: method=flat, checking its content hash directly")
        return _check_via_content_hash(
            fod,
            client,
            nix_binary=nix_binary,
            swh_binary=swh_binary,
            swh_identify_timeout=swh_identify_timeout,
            disarchive_timeout=disarchive_timeout,
            disarchive_db_url=disarchive_db_url,
            skip_disarchive=skip_disarchive,
            cache=cache,
            on_log=on_log,
        )

    if on_log:
        on_log(
            f"{fod.label}: method={fod.method!r} has no direct Software Heritage "
            "equivalent, realising it to compute its actual SWHID"
        )
    return _check_via_build_and_identify(
        fod,
        client,
        nix_binary=nix_binary,
        swh_binary=swh_binary,
        swh_identify_timeout=swh_identify_timeout,
        disarchive_timeout=disarchive_timeout,
        cache=cache,
        on_log=on_log,
    )


def _check_via_content_hash(
    fod: FixedOutputDerivation,
    client: SWHClient,
    *,
    nix_binary: str = "nix",
    swh_binary: str = "swh",
    swh_identify_timeout: float = 30.0,
    disarchive_timeout: float = 30.0,
    disarchive_db_url: str = _DISARCHIVE_DB_URL,
    skip_disarchive: bool = False,
    cache: Cache | None = None,
    on_log: Callable[[str], None] | None = None,
) -> SWHCheckResult:
    result = client.lookup_content(fod.hash_algo, fod.hash_hex)
    swhid: str | None = None
    swh_url: str | None = None
    if result.known and result.raw:
        sha1_git = result.raw.get("checksums", {}).get("sha1_git")
        if sha1_git:
            swhid = f"swh:1:cnt:{sha1_git}"
            swh_url = f"{_ARCHIVE_URL}/{swhid}"
    if result.known:
        return SWHCheckResult(
            fod=fod,
            known=True,
            method=SWHLookupMethod.CONTENT_HASH,
            detail=f"content lookup by {fod.hash_algo}:{fod.hash_hex}",
            swhid=swhid,
            swh_url=swh_url,
            origin_urls=fod.origin_urls,
        )
    disarchive_result = try_disarchive(
        fod,
        client,
        nix_binary=nix_binary,
        swh_binary=swh_binary,
        swh_identify_timeout=swh_identify_timeout,
        disarchive_timeout=disarchive_timeout,
        disarchive_db_url=disarchive_db_url,
        skip_disarchive=skip_disarchive,
        cache=cache,
        on_log=on_log,
    )
    if disarchive_result is not None:
        if disarchive_result.origin_urls is None:
            disarchive_result.origin_urls = []
        disarchive_result.origin_urls = list(
            dict.fromkeys(disarchive_result.origin_urls + fod.origin_urls)
        )
        return disarchive_result
    # try_disarchive returning None means the FOD could not be realised,
    # is not an archive, or could not be unpacked. This is not the same as
    # Software Heritage not knowing the content, so report it as undetermined.
    return SWHCheckResult(
        fod=fod,
        known=None,
        method=SWHLookupMethod.UNDETERMINED,
        detail=f"content lookup by {fod.hash_algo}:{fod.hash_hex} failed; could not realise or unpack the FOD for disarchive",
        origin_urls=fod.origin_urls,
    )


def _check_via_swhid(
    fod: FixedOutputDerivation,
    client: SWHClient,
    *,
    nix_binary: str = "nix",
    swh_binary: str = "swh",
    swh_identify_timeout: float = 30.0,
    disarchive_timeout: float = 30.0,
    disarchive_db_url: str = _DISARCHIVE_DB_URL,
    skip_disarchive: bool = False,
    cache: Cache | None = None,
    on_log: Callable[[str], None] | None = None,
) -> SWHCheckResult:
    # We don't know upfront whether the FOD output is a single file (SWH
    # "content") or a directory (SWH "directory"), so probe both.
    candidates = [f"swh:1:cnt:{fod.hash_hex}", f"swh:1:dir:{fod.hash_hex}"]
    known_map = client.lookup_known_swhids(candidates)
    known_swhids = [swhid for swhid, known in known_map.items() if known]
    if known_swhids:
        swhid = known_swhids[0]
        return SWHCheckResult(
            fod=fod,
            known=True,
            method=SWHLookupMethod.SWHID_KNOWN,
            detail=f"known as {', '.join(known_swhids)}",
            swhid=swhid,
            swh_url=f"{_ARCHIVE_URL}/{swhid}",
            origin_urls=fod.origin_urls,
        )
    disarchive_result = try_disarchive(
        fod,
        client,
        nix_binary=nix_binary,
        swh_binary=swh_binary,
        swh_identify_timeout=swh_identify_timeout,
        disarchive_timeout=disarchive_timeout,
        disarchive_db_url=disarchive_db_url,
        skip_disarchive=skip_disarchive,
        cache=cache,
        on_log=on_log,
    )
    if disarchive_result is not None:
        if disarchive_result.origin_urls is None:
            disarchive_result.origin_urls = []
        disarchive_result.origin_urls = list(
            dict.fromkeys(disarchive_result.origin_urls + fod.origin_urls)
        )
        return disarchive_result
    # try_disarchive returning None means the FOD could not be realised,
    # is not an archive, or could not be unpacked. This is not the same as
    # Software Heritage not knowing the content, so report it as undetermined.
    return SWHCheckResult(
        fod=fod,
        known=None,
        method=SWHLookupMethod.UNDETERMINED,
        detail=f"neither {candidates[0]} nor {candidates[1]} are known; could not realise or unpack the FOD for disarchive",
        origin_urls=fod.origin_urls,
    )


def _check_via_build_and_identify(
    fod: FixedOutputDerivation,
    client: SWHClient,
    *,
    nix_binary: str,
    swh_binary: str,
    swh_identify_timeout: float = 30.0,
    disarchive_timeout: float = 30.0,
    cache: Cache | None = None,
    on_log: Callable[[str], None] | None = None,
) -> SWHCheckResult:
    cache_key_prefix = fod.output_path or ""

    # If the SWHID and its /known status are already cached, skip realisation.
    if cache is not None and cache_key_prefix:
        swhid_cached = cache.get(f"tool:swh_identify:{cache_key_prefix}")
        if swhid_cached is not None:
            swhid = swhid_cached.get("swhid")
            if swhid:
                if on_log:
                    on_log(f"{fod.label}: using cached SWHID {swhid}")
                known = client.lookup_known_swhids([swhid]).get(swhid, False)
                return SWHCheckResult(
                    fod=fod,
                    known=known,
                    method=SWHLookupMethod.BUILD_AND_IDENTIFY,
                    detail=f"cached SWHID {swhid}",
                    swhid=swhid,
                    swh_url=f"{_ARCHIVE_URL}/{swhid}" if known else None,
                    origin_urls=fod.origin_urls,
                )

    try:
        out_path = realise_fod(fod, nix_binary=nix_binary, on_log=on_log)
    except NixCommandError as exc:
        return SWHCheckResult(
            fod=fod,
            known=None,
            method=SWHLookupMethod.UNDETERMINED,
            detail=f"could not realise FOD to compute its SWHID: {exc}",
            origin_urls=fod.origin_urls,
        )

    try:
        swhid = compute_swhid(
            out_path,
            swh_binary=swh_binary,
            on_log=on_log,
            timeout=swh_identify_timeout,
            cache=cache,
            cache_key=cache_key_prefix or out_path,
        )
    except SWHIdentifyError as exc:
        return SWHCheckResult(
            fod=fod,
            known=None,
            method=SWHLookupMethod.UNDETERMINED,
            detail=f"built {out_path} but could not compute its SWHID: {exc}",
            origin_urls=fod.origin_urls,
        )

    known = client.lookup_known_swhids([swhid]).get(swhid, False)
    return SWHCheckResult(
        fod=fod,
        known=known,
        method=SWHLookupMethod.BUILD_AND_IDENTIFY,
        detail=f"built {out_path} and computed {swhid}",
        swhid=swhid,
        swh_url=f"{_ARCHIVE_URL}/{swhid}" if known else None,
        origin_urls=fod.origin_urls,
    )
