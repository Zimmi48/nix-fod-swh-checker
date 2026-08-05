"""A small client for the Software Heritage Web API.

See https://docs.softwareheritage.org/devel/swh-web/api/ for the full API
reference. Only the handful of read-only endpoints needed to check whether a
given content hash, SWHID, or origin URL is already archived are implemented
here.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable

import requests

DEFAULT_API_URL = "https://archive.softwareheritage.org/api/1"

# Hash algorithms accepted by the SWH `content` lookup endpoint (raw content
# checksums, as opposed to the git-flavoured `sha1_git`).
CONTENT_LOOKUP_ALGOS = {"sha1", "sha1_git", "sha256", "blake2s256"}


class SWHError(RuntimeError):
    """Raised on unexpected errors talking to the Software Heritage API."""


@dataclass
class ContentLookupResult:
    known: bool
    raw: dict | None = None


class SWHClient:
    """Thin wrapper around the Software Heritage Web API with basic
    rate-limiting and retry-on-429 support.
    """

    def __init__(
        self,
        api_url: str = DEFAULT_API_URL,
        *,
        api_token: str | None = None,
        timeout: float = 20.0,
        max_retries: int = 3,
        min_delay: float = 1.0,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.min_delay = min_delay
        self._last_request_time = 0.0

        self.session = requests.Session()
        self.session.headers["Accept"] = "application/json"
        if api_token:
            self.session.headers["Authorization"] = f"Bearer {api_token}"

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < self.min_delay:
            time.sleep(self.min_delay - elapsed)

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.api_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                response = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(self.min_delay * (attempt + 1))
                continue
            self._last_request_time = time.monotonic()
            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", self.min_delay * 2))
                time.sleep(retry_after)
                continue
            return response
        raise SWHError(f"request to {url} failed after {self.max_retries + 1} attempts") from last_exc

    def lookup_content(self, algo: str, hash_hex: str) -> ContentLookupResult:
        """Check whether a content object with the given checksum is archived.

        Corresponds to `GET /content/{algo}:{hash}/`.
        """
        if algo not in CONTENT_LOOKUP_ALGOS:
            raise ValueError(f"unsupported content lookup algorithm: {algo}")
        response = self._request("GET", f"/content/{algo}:{hash_hex}/")
        if response.status_code == 404:
            return ContentLookupResult(known=False)
        if response.status_code == 200:
            return ContentLookupResult(known=True, raw=response.json())
        raise SWHError(
            f"unexpected status {response.status_code} looking up content {algo}:{hash_hex}"
        )

    def lookup_known_swhids(self, swhids: Iterable[str]) -> dict[str, bool]:
        """Check whether a batch of SWHIDs are known to the archive.

        Corresponds to `POST /known/`.
        """
        swhids = list(swhids)
        if not swhids:
            return {}
        response = self._request("POST", "/known/", json=swhids)
        if response.status_code != 200:
            raise SWHError(f"unexpected status {response.status_code} calling /known/")
        data = response.json()
        return {swhid: bool(info.get("known")) for swhid, info in data.items()}

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "SWHClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
