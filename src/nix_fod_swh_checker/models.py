"""Data models shared across the nix-fod-swh-checker package."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SWHLookupMethod(str, Enum):
    """The strategy used to compare a FOD against the Software Heritage archive."""

    CONTENT_HASH = "content_hash"
    SWHID_KNOWN = "swhid_known"
    ORIGIN_URL = "origin_url"
    UNSUPPORTED = "unsupported"


@dataclass
class FixedOutputDerivation:
    """A single fixed-output derivation (FOD) output extracted from the JSON
    produced by `nix derivation show --recursive`.
    """

    drv_path: str
    output_name: str
    output_path: str | None
    name: str
    method: str | None
    hash_algo: str | None
    hash_hex: str | None
    urls: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        suffix = f"^{self.output_name}" if self.output_name != "out" else ""
        return f"{self.drv_path}{suffix}"


@dataclass
class SWHCheckResult:
    """The outcome of checking one FOD against Software Heritage."""

    fod: FixedOutputDerivation
    known: bool | None  # None means "could not be determined"
    method: SWHLookupMethod
    detail: str
    swh_url: str | None = None
