"""Shared cache for API responses and expensive tool runs.

The cache is stored separately from per-installable checkpoints so that
results can be reused across attributes. Positive API responses and
successful tool runs are cached forever; negative API responses are cached
for a limited time and ignored when the user asks to retry unknown or
undetermined FODs.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

CACHE_VERSION = 1
DEFAULT_MISS_TTL_SECONDS = 86400.0  # 1 day


def default_cache_path() -> Path:
    """Return the default path for the shared cache file.

    The file lives under ``$XDG_CACHE_HOME`` (or ``$HOME/.cache``) so that
    different installables share the same cached API responses and tool
    outputs.
    """
    cache_home = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    return Path(cache_home) / "nix-fod-swh-checker" / "cache.json"


@dataclass
class Cache:
    """A simple file-backed cache with TTL support for negative entries."""

    path: Path
    miss_ttl_seconds: float = DEFAULT_MISS_TTL_SECONDS
    ignore_misses: bool = False
    _entries: dict[str, dict] = field(default_factory=dict, init=False)
    _dirty: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self._load()

    def _load(self) -> None:
        """Load existing entries, dropping expired negative entries."""
        try:
            raw = json.loads(self.path.read_text())
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return

        if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
            return

        entries = raw.get("entries")
        if not isinstance(entries, dict):
            return

        now = time.time()
        for key, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            kind = entry.get("kind")
            if kind == "miss":
                created = entry.get("created")
                if not isinstance(created, (int, float)):
                    continue
                if self.ignore_misses or (now - created) > self.miss_ttl_seconds:
                    self._dirty = True
                    continue
            elif kind != "hit":
                continue
            self._entries[key] = entry

    def get(self, key: str) -> dict | None:
        """Return the cached value for ``key``, or ``None``.

        Negative entries are ignored when ``ignore_misses`` is set or when
        their TTL has expired.
        """
        entry = self._entries.get(key)
        if entry is None:
            return None

        kind = entry.get("kind")
        if kind == "miss":
            created = entry.get("created", 0)
            if self.ignore_misses or (time.time() - created) > self.miss_ttl_seconds:
                self._entries.pop(key, None)
                self._dirty = True
                return None

        return entry.get("value")

    def get_tool_result(self, key: str, timeout: float) -> dict | None:
        """Return a cached tool result, respecting timeout-aware invalidation.

        Successful tool runs are always reused. Entries that record a timeout
        miss are only reused when the current ``timeout`` is less than or
        equal to the timeout stored in the cache. This lets a user increase
        the timeout and re-run a tool that previously timed out, while still
        avoiding redundant runs when the timeout is unchanged or lowered.
        """
        value = self.get(key)
        if value is None:
            return None
        if value.get("timed_out"):
            stored_timeout = value.get("timeout")
            if stored_timeout is not None and timeout > stored_timeout:
                return None
        return value

    def set(self, key: str, value: dict, *, is_miss: bool = False) -> None:
        """Store ``value`` under ``key``.

        When ``is_miss`` is ``True`` the entry is recorded with the current
        time and will expire after ``miss_ttl_seconds``.
        """
        entry: dict = {"kind": "miss" if is_miss else "hit", "value": value}
        if is_miss:
            entry["created"] = time.time()
        self._entries[key] = entry
        self._dirty = True

    def delete(self, key: str) -> None:
        """Remove ``key`` from the cache if present."""
        if key in self._entries:
            del self._entries[key]
            self._dirty = True

    def save(self) -> None:
        """Atomically write the cache to disk if it has changed."""
        if not self._dirty:
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": CACHE_VERSION, "entries": self._entries}

        fd, tmp_name = tempfile.mkstemp(
            dir=self.path.parent, prefix=".tmp-cache-", suffix=".json"
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp_name, self.path)
            self._dirty = False
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
