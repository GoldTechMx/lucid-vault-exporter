"""Proactive window-bucket rate limiting for Lucid's per-account 5-second windows.

Documented limits: document export 75 req/5 s, document search 300 req/5 s, per ACCOUNT
(not per token) - so defaults in config stay well under. Lucid signals overrun with
HTTP 429 + Retry-After (seconds); there are no remaining/reset headers on success, so the
buckets are the primary control and 429 handling is the safety net. Time and sleep are
injectable for deterministic tests. Thread-safe: the web UI runs export in a worker thread.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger("lucid_vault_exporter.ratelimit")

_WINDOW = 5.0
_EPS = 1e-6


@dataclass
class _Bucket:
    capacity: float
    refill_per_sec: float
    tokens: float
    last: float


class RateLimiter:
    def __init__(
        self,
        budgets: dict[str, int] | None = None,
        *,
        default_per_5s: int = 60,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._limits = dict(budgets or {})
        self._default = max(1, default_per_5s)
        self._mono = monotonic
        self._sleep = sleep
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}
        self._pause_until: dict[str, float] = {}  # monotonic deadline per budget

    def _bucket(self, budget: str) -> _Bucket:
        b = self._buckets.get(budget)
        if b is None:
            cap = float(self._limits.get(budget, self._default))
            b = _Bucket(cap, cap / _WINDOW, cap, self._mono())
            self._buckets[budget] = b
        return b

    def acquire(self, budget: str = "default") -> None:
        """Block until a request slot is available for this budget."""
        while True:
            with self._lock:
                now = self._mono()
                wait = self._pause_until.get(budget, 0.0) - now
                if wait <= 0:
                    b = self._bucket(budget)
                    b.tokens = min(b.capacity, b.tokens + (now - b.last) * b.refill_per_sec)
                    b.last = now
                    if b.tokens >= 1.0 - _EPS:
                        b.tokens -= 1
                        return
                    wait = (1.0 - b.tokens) / b.refill_per_sec
            self._sleep(min(wait, 1.0))

    def note_throttled(self, budget: str, retry_after: float | None) -> float:
        """A 429 arrived: pause this budget for Retry-After seconds (default 60)."""
        wait = retry_after if retry_after and retry_after > 0 else 60.0
        with self._lock:
            deadline = self._mono() + wait
            self._pause_until[budget] = max(self._pause_until.get(budget, 0.0), deadline)
        log.info("429 on %s budget; pausing %.0fs.", budget, wait)
        return wait
