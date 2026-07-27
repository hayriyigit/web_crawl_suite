"""Crawl counters, throughput and latency sampling.

Everything here is O(1) per page and allocation-free on the hot path, so
metrics stay on even under load.
"""

from __future__ import annotations

import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Metrics"]


@dataclass(slots=True)
class Metrics:
    """Running crawl statistics."""

    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None

    fetched: int = 0
    succeeded: int = 0
    failed: int = 0
    retried: int = 0
    discovered: int = 0
    enqueued: int = 0
    duplicates: int = 0
    out_of_scope: int = 0
    robots_blocked: int = 0
    bytes_html: int = 0

    status_counts: Counter[int] = field(default_factory=Counter)
    error_counts: Counter[str] = field(default_factory=Counter)

    #: Sliding window of (monotonic time, ok?) used for live rate figures.
    _window: deque[tuple[float, bool]] = field(default_factory=lambda: deque(maxlen=2000))
    #: Reservoir of recent fetch durations for percentile estimates.
    _latencies: deque[float] = field(default_factory=lambda: deque(maxlen=2000))

    def record_fetch(
        self, *, ok: bool, http_status: int | None, elapsed: float, nbytes: int = 0
    ) -> None:
        now = time.monotonic()
        self.fetched += 1
        if ok:
            self.succeeded += 1
        else:
            self.failed += 1
        if http_status is not None:
            self.status_counts[http_status] += 1
        self.bytes_html += nbytes
        self._window.append((now, ok))
        if elapsed > 0:
            self._latencies.append(elapsed)

    def record_error(self, kind: str) -> None:
        self.error_counts[kind] += 1

    @property
    def elapsed(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return max(1e-9, end - self.started_at)

    @property
    def pages_per_second(self) -> float:
        return self.fetched / self.elapsed

    @property
    def recent_pages_per_second(self) -> float:
        """Throughput over the sliding window -- reacts to slowdowns."""
        if len(self._window) < 2:
            return self.pages_per_second
        span = self._window[-1][0] - self._window[0][0]
        return len(self._window) / span if span > 1e-6 else 0.0

    @property
    def success_rate(self) -> float:
        return self.succeeded / self.fetched if self.fetched else 0.0

    def latency_percentiles(self) -> dict[str, float]:
        if not self._latencies:
            return {"p50": 0.0, "p90": 0.0, "p99": 0.0}
        s = sorted(self._latencies)
        n = len(s)

        def pct(p: float) -> float:
            return s[min(n - 1, int(p * n))]

        return {"p50": pct(0.50), "p90": pct(0.90), "p99": pct(0.99)}

    def finish(self) -> None:
        self.finished_at = time.monotonic()

    def summary(self) -> dict[str, Any]:
        lat = self.latency_percentiles()
        return {
            "elapsed_s": round(self.elapsed, 2),
            "fetched": self.fetched,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "retried": self.retried,
            "discovered": self.discovered,
            "enqueued": self.enqueued,
            "duplicates": self.duplicates,
            "out_of_scope": self.out_of_scope,
            "robots_blocked": self.robots_blocked,
            "pages_per_second": round(self.pages_per_second, 2),
            "success_rate": round(self.success_rate, 4),
            "mb_html": round(self.bytes_html / 1e6, 2),
            "latency_p50_s": round(lat["p50"], 3),
            "latency_p90_s": round(lat["p90"], 3),
            "latency_p99_s": round(lat["p99"], 3),
            "status_counts": dict(sorted(self.status_counts.items())),
            "errors": dict(self.error_counts.most_common(10)),
        }
