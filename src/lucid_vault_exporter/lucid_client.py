"""Thin, strictly read-only Lucid REST client.

Every call: acquire() the right rate budget first; on 429 honor Retry-After via the
limiter and retry (max 6 attempts); on 5xx exponential backoff. Pagination follows the
`Link: <url>; rel="next"` response header. The Bearer token comes from a provider callable
so OAuth refresh stays out of this module. Tokens are never logged.

NOTE: response shapes (bare JSON array for search/folder contents, Link-header pagination,
document field names) are PROVISIONAL and reconciled against the live API in Task 15.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from .ratelimit import RateLimiter

log = logging.getLogger("lucid_vault_exporter.client")

_LINK_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')
_MAX_ATTEMPTS = 6
PAGE_SIZE = 200


class LucidError(Exception):
    pass


class PageNotFound(LucidError):
    """Requested page number is beyond the document's last page."""


class LucidClient:
    def __init__(
        self,
        api_base: str,
        *,
        token_provider: Callable[[], str],
        ratelimiter: RateLimiter,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 60.0,
    ) -> None:
        self._base = api_base.rstrip("/")
        self._token = token_provider
        self._rl = ratelimiter
        self._sleep = sleep
        self._http = httpx.Client(timeout=timeout, follow_redirects=True)

    def close(self) -> None:
        self._http.close()

    # -- core request with retry/backoff ---------------------------------------------------
    def _request(
        self, method: str, url: str, *, budget: str,
        accept: str = "application/json", json_body: Any | None = None,
    ) -> httpx.Response:
        last: httpx.Response | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            self._rl.acquire(budget)
            headers = {
                "Authorization": f"Bearer {self._token()}",
                "Lucid-Api-Version": "1",
                "Accept": accept,
            }
            try:
                resp = self._http.request(method, url, headers=headers, json=json_body)
            except httpx.HTTPError as exc:
                if attempt == _MAX_ATTEMPTS:
                    raise LucidError(f"Network error after {attempt} attempts: {exc}") from exc
                self._sleep(min(2 ** attempt, 30))
                continue
            if resp.status_code == 429:
                # note_throttled records a pause deadline on the limiter (so other threads
                # sharing this budget also back off) AND returns the wait we sleep here for
                # this thread. In the common case these coincide, so it's one effective wait.
                retry_after = _to_float(resp.headers.get("Retry-After"))
                self._sleep(self._rl.note_throttled(budget, retry_after))
                last = resp
                continue
            if resp.status_code >= 500:
                last = resp
                if attempt == _MAX_ATTEMPTS:
                    break
                self._sleep(min(2 ** attempt, 30))
                continue
            return resp
        raise LucidError(
            f"{method} {url} failed after {_MAX_ATTEMPTS} attempts "
            f"(last HTTP {last.status_code if last else '???'})"
        )

    # -- endpoints --------------------------------------------------------------------------
    def search_documents(
        self, *, products: list[str], exclude_trashed: bool = True,
    ) -> Iterator[dict[str, Any]]:
        url = f"{self._base}/documents/search?pageSize={PAGE_SIZE}"
        body = {"product": products}
        while url:
            resp = self._request("POST", url, budget="search", json_body=body)
            if resp.status_code != 200:
                raise LucidError(f"documents/search -> HTTP {resp.status_code}: {resp.text[:200]}")
            for doc in resp.json():
                if exclude_trashed and doc.get("trashed"):
                    continue
                yield doc
            m = _LINK_NEXT.search(resp.headers.get("Link", ""))
            url = m.group(1) if m else ""

    def get_document(self, doc_id: str) -> dict[str, Any]:
        resp = self._request("GET", f"{self._base}/documents/{doc_id}", budget="search")
        if resp.status_code != 200:
            raise LucidError(f"documents/{doc_id} -> HTTP {resp.status_code}")
        return dict(resp.json())

    def export_page_png(self, doc_id: str, *, page: int) -> bytes:
        resp = self._request(
            "GET", f"{self._base}/documents/{doc_id}?page={page}",
            budget="export", accept="image/png",
        )
        if resp.status_code == 404:
            raise PageNotFound(f"{doc_id} page {page}")
        if resp.status_code == 403:
            raise LucidError(f"403 forbidden exporting {doc_id} (no export permission)")
        if resp.status_code != 200:
            raise LucidError(f"export {doc_id} p{page} -> HTTP {resp.status_code}")
        return resp.content

    def folder_contents(self, folder_id: str) -> Iterator[dict[str, Any]]:
        url = f"{self._base}/folders/{folder_id}/contents?pageSize={PAGE_SIZE}"
        while url:
            resp = self._request("GET", url, budget="search")
            if resp.status_code == 403:
                return  # deleted/no-access folder: caller records and moves on
            if resp.status_code != 200:
                raise LucidError(f"folders/{folder_id}/contents -> HTTP {resp.status_code}")
            yield from resp.json()
            m = _LINK_NEXT.search(resp.headers.get("Link", ""))
            url = m.group(1) if m else ""

    def get_folder(self, folder_id: str) -> dict[str, Any] | None:
        resp = self._request("GET", f"{self._base}/folders/{folder_id}", budget="search")
        if resp.status_code in (403, 404):
            return None
        if resp.status_code != 200:
            raise LucidError(f"folders/{folder_id} -> HTTP {resp.status_code}")
        return dict(resp.json())


def _to_float(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
