"""A small client for the Software Heritage Web API.

See https://docs.softwareheritage.org/devel/swh-web/api/ for the full API
reference. Only the handful of read-only endpoints needed to check whether a
given content hash, SWHID, or origin URL is already archived are implemented
here.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterable

import requests

DEFAULT_API_URL = "https://archive.softwareheritage.org/api/1"

# Hash algorithms accepted by the SWH `content` lookup endpoint (raw content
# checksums, as opposed to the git-flavoured `sha1_git`).
CONTENT_LOOKUP_ALGOS = {"sha1", "sha1_git", "sha256", "blake2s256"}


class SWHError(RuntimeError):
    """Raised on unexpected errors talking to the Software Heritage API."""


class VaultCookingError(SWHError):
    """Raised when a vault cooking task fails or times out."""


@dataclass
class ContentLookupResult:
    known: bool
    raw: dict | None = None


@dataclass
class VaultCookingTask:
    """Status of a Software Heritage vault flat cooking task."""

    id: int
    swhid: str
    status: str
    progress_message: str
    fetch_url: str | None = None
    raw: dict | None = None


@dataclass
class SaveRequest:
    """Status of a Software Heritage origin save request."""

    id: int
    origin_url: str
    visit_type: str
    save_request_status: str
    save_task_status: str
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
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.min_delay = min_delay
        self.on_log = on_log
        self._last_request_time = 0.0

        self.session = requests.Session()
        self.session.headers["Accept"] = "application/json"
        if api_token:
            self.session.headers["Authorization"] = f"Bearer {api_token}"
            self.logged_in = True
        else:
            self.logged_in = False

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < self.min_delay:
            wait = self.min_delay - elapsed
            if self.on_log and wait > 0.05:
                self.on_log(f"waiting {wait:.1f}s before the next Software Heritage API request...")
            time.sleep(wait)

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.api_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if not self.logged_in:
                self._throttle()
            try:
                response = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.RequestException as exc:
                last_exc = exc
                if self.on_log:
                    self.on_log(f"request to {url} failed ({exc}), retrying...")
                time.sleep(self.min_delay * (attempt + 1))
                continue
            self._last_request_time = time.monotonic()
            if response.status_code == 429:
                retry_after = self._retry_after_seconds(response)
                if self.on_log:
                    self.on_log(
                        f"rate-limited by the Software Heritage API, "
                        f"retrying in {retry_after:.1f}s..."
                    )
                time.sleep(retry_after)
                continue
            self._warn_if_quota_low(response)
            return response
        raise SWHError(f"request to {url} failed after {self.max_retries + 1} attempts") from last_exc

    def _retry_after_seconds(self, response: requests.Response) -> float:
        """Determine how long to wait before retrying a 429 response.

        The Software Heritage API does not send a standard `Retry-After`
        header; instead every response carries `X-RateLimit-Limit`,
        `X-RateLimit-Remaining`, and `X-RateLimit-Reset` (a Unix timestamp for
        when the current throttling window resets), confirmed by inspecting
        a live response. `Retry-After` is honoured too in case that ever
        changes, with a fixed fallback if neither header is present.
        """
        reset = response.headers.get("X-RateLimit-Reset")
        if reset:
            try:
                return max(0.0, float(reset) - time.time())
            except ValueError:
                pass
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        return self.min_delay * 2

    def _warn_if_quota_low(self, response: requests.Response, threshold: int = 3) -> None:
        if not self.on_log:
            return
        remaining = response.headers.get("X-RateLimit-Remaining")
        reset = response.headers.get("X-RateLimit-Reset")
        if remaining is None:
            return
        try:
            remaining_count = int(remaining)
        except ValueError:
            return
        if remaining_count > threshold:
            return
        wait = f"{max(0.0, float(reset) - time.time()):.0f}s" if reset else "unknown"
        self.on_log(
            f"Software Heritage API quota running low ({remaining_count} request(s) "
            f"remaining, resets in {wait})"
        )

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

    def cook_vault_flat(self, swhid: str) -> VaultCookingTask:
        """Request the cooking of a vault flat archive for ``swhid``.

        Corresponds to `POST /vault/flat/{swhid}/`.
        """
        response = self._request("POST", f"/vault/flat/{swhid}/")
        if response.status_code not in (200, 201):
            raise SWHError(
                f"unexpected status {response.status_code} requesting vault flat cooking for {swhid}"
            )
        data = response.json()
        return self._task_from_json(data)

    def get_vault_flat_task(self, swhid: str) -> VaultCookingTask | None:
        """Check the status of a vault flat cooking task for ``swhid``.

        Corresponds to `GET /vault/flat/{swhid}/`. Returns ``None`` if no
        cooking task has been requested yet for this SWHID.
        """
        response = self._request("GET", f"/vault/flat/{swhid}/")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise SWHError(
                f"unexpected status {response.status_code} checking vault flat task for {swhid}"
            )
        return self._task_from_json(response.json())

    def ensure_vault_flat_cooking(self, swhid: str) -> VaultCookingTask:
        """Request cooking of a vault flat archive if no task exists yet.

        If a task already exists for ``swhid``, its current status is returned
        without making a POST request. Otherwise, a new cooking task is
        created. This is a fire-and-forget helper: it does not wait for the
        task to complete.
        """
        task = self.get_vault_flat_task(swhid)
        if task is not None:
            return task
        return self.cook_vault_flat(swhid)

    def request_origin_save(
        self,
        origin_url: str,
        *,
        visit_type: str = "tarball",
    ) -> SaveRequest:
        """Request the archiving of a software origin.

        Corresponds to ``POST /origin/save/`` with ``origin_url`` and
        ``visit_type`` query parameters.  The default ``visit_type`` is
        ``tarball`` for simple file downloads; ``git`` should be used for
        version-control origins.
        """
        response = self._request(
            "POST",
            "/origin/save/",
            params={"origin_url": origin_url, "visit_type": visit_type},
        )
        if response.status_code == 403:
            raise SWHError(f"origin URL {origin_url!r} is blocked by Software Heritage")
        if response.status_code == 400:
            raise SWHError(
                f"invalid origin URL {origin_url!r} or visit type {visit_type!r}"
            )
        if response.status_code not in (200, 201):
            raise SWHError(
                f"unexpected status {response.status_code} requesting save for {origin_url!r}"
            )
        data = response.json()
        return self._save_request_from_json(data)

    def get_origin_save_request(self, request_id: int) -> SaveRequest:
        """Get the status of a specific origin save request.

        Corresponds to ``GET /origin/save/{request_id}/``.
        """
        response = self._request("GET", f"/origin/save/{request_id}/")
        if response.status_code == 404:
            raise SWHError(f"save request {request_id} not found")
        if response.status_code != 200:
            raise SWHError(
                f"unexpected status {response.status_code} checking save request {request_id}"
            )
        data = response.json()
        return self._save_request_from_json(data)

    @staticmethod
    def _save_request_from_json(data: dict) -> SaveRequest:
        return SaveRequest(
            id=data["id"],
            origin_url=data["origin_url"],
            visit_type=data["visit_type"],
            save_request_status=data["save_request_status"],
            save_task_status=data["save_task_status"],
            raw=data,
        )

    def wait_for_vault_flat(
        self,
        swhid: str,
        *,
        timeout: float = 600.0,
        poll_interval: float = 5.0,
    ) -> VaultCookingTask:
        """Request (or wait for) a vault flat archive to be cooked.

        If no cooking task exists yet for ``swhid``, one is created.  The
        function then polls the task status until it is ``done`` or ``failed``,
        or until ``timeout`` seconds have elapsed.
        """
        deadline = time.monotonic() + timeout
        task = self.ensure_vault_flat_cooking(swhid)

        while task.status not in ("done", "failed"):
            if time.monotonic() > deadline:
                raise VaultCookingError(
                    f"timed out after {timeout}s waiting for vault flat task {task.id} "
                    f"for {swhid} (status: {task.status})"
                )
            if self.on_log:
                self.on_log(
                    f"vault flat task {task.id} for {swhid}: {task.status} "
                    f"({task.progress_message}); polling..."
                )
            time.sleep(poll_interval)
            task = self.get_vault_flat_task(swhid)
            if task is None:
                raise VaultCookingError(
                    f"vault flat task for {swhid} disappeared while waiting"
                )

        if task.status == "failed":
            raise VaultCookingError(
                f"vault flat task {task.id} for {swhid} failed: {task.progress_message}"
            )

        return task

    @staticmethod
    def _task_from_json(data: dict) -> VaultCookingTask:
        return VaultCookingTask(
            id=data["id"],
            swhid=data["swhid"],
            status=data["status"],
            progress_message=data.get("progress_message", ""),
            fetch_url=data.get("fetch_url"),
            raw=data,
        )

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "SWHClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
