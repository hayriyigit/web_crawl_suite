"""Per-host politeness: token buckets plus adaptive rate control.

A crawler that is fast in aggregate but rude to a single origin gets blocked,
and a blocked origin costs far more throughput than the rate limit ever did.
This module keeps a bucket per host and moves each host's allowance up and down
based on the responses that host actually returns (AIMD -- additive increase on
success, multiplicative decrease on throttle signals), which is the same
control law TCP congestion control uses and for the same reason: it converges
on the highest rate the origin will tolerate without needing to know it ahead
of time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

__all__ = ["HostBucket", "HostLimiter"]


@dataclass(slots=True)
class HostBucket:
    """Token bucket + adaptive rate state for one host."""

    rate: float
    burst: float
    tokens: float
    updated: float
    min_rate: float
    max_rate: float
    #: Earliest monotonic time this host may be contacted (backoff / Crawl-delay).
    blocked_until: float = 0.0
    consecutive_ok: int = 0
    throttle_events: int = 0

    def _refill(self, now: float) -> None:
        elapsed = now - self.updated
        if elapsed > 0:
            self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
            self.updated = now

    def try_acquire(self, now: float) -> bool:
        if now < self.blocked_until:
            return False
        self._refill(now)
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False

    def ready_at(self, now: float) -> float:
        """Monotonic time at which this host will next have a token."""
        if now < self.blocked_until:
            return self.blocked_until
        self._refill(now)
        if self.tokens >= 1.0:
            return now
        deficit = 1.0 - self.tokens
        return now + (deficit / self.rate if self.rate > 0 else 3600.0)


class HostLimiter:
    """Adaptive per-host rate limiter.

    ``rate`` starts at ``base_rate`` for each newly seen host and is then
    steered by :meth:`on_success` / :meth:`on_throttled`.
    """

    __slots__ = (
        "_buckets",
        "_crawl_delay",
        "base_rate",
        "burst",
        "decrease_factor",
        "increase_step",
        "max_rate",
        "min_rate",
    )

    def __init__(
        self,
        *,
        base_rate: float = 4.0,
        burst: float = 8.0,
        min_rate: float = 0.2,
        max_rate: float = 32.0,
        increase_step: float = 0.5,
        decrease_factor: float = 0.5,
    ) -> None:
        self.base_rate = base_rate
        self.burst = burst
        self.min_rate = min_rate
        self.max_rate = max_rate
        self.increase_step = increase_step
        self.decrease_factor = decrease_factor
        self._buckets: dict[str, HostBucket] = {}
        self._crawl_delay: dict[str, float] = {}

    def bucket(self, host: str) -> HostBucket:
        b = self._buckets.get(host)
        if b is None:
            rate = self.base_rate
            # A robots.txt Crawl-delay is a hard ceiling, never exceeded.
            delay = self._crawl_delay.get(host)
            if delay:
                rate = min(rate, 1.0 / delay)
            b = HostBucket(
                rate=rate,
                burst=max(1.0, min(self.burst, rate * 2)),
                tokens=max(1.0, min(self.burst, rate * 2)),
                updated=time.monotonic(),
                min_rate=self.min_rate,
                max_rate=self.max_rate,
            )
            self._buckets[host] = b
        return b

    def set_crawl_delay(self, host: str, delay: float | None) -> None:
        """Apply a robots.txt ``Crawl-delay`` as an upper bound on the host rate."""
        if not delay or delay <= 0:
            return
        self._crawl_delay[host] = delay
        capped = 1.0 / delay
        b = self._buckets.get(host)
        if b is not None:
            b.rate = min(b.rate, capped)
            b.max_rate = min(b.max_rate, capped)
            b.burst = max(1.0, min(b.burst, capped * 2))

    def try_acquire(self, host: str) -> bool:
        return self.bucket(host).try_acquire(time.monotonic())

    def ready_at(self, host: str) -> float:
        return self.bucket(host).ready_at(time.monotonic())

    def on_success(self, host: str) -> None:
        """Additive increase, but only after a run of clean responses.

        Requiring a streak keeps the rate from ratcheting straight back up
        after a 429, which would just re-trigger the block.
        """
        b = self.bucket(host)
        b.consecutive_ok += 1
        if b.consecutive_ok >= 10 and b.rate < b.max_rate:
            b.rate = min(b.max_rate, b.rate + self.increase_step)
            b.burst = max(1.0, min(self.burst, b.rate * 2))
            b.consecutive_ok = 0

    def on_throttled(self, host: str, retry_after: float | None = None) -> None:
        """Multiplicative decrease after a 429/503, plus an explicit pause."""
        b = self.bucket(host)
        b.consecutive_ok = 0
        b.throttle_events += 1
        b.rate = max(b.min_rate, b.rate * self.decrease_factor)
        b.burst = max(1.0, min(self.burst, b.rate * 2))
        b.tokens = 0.0
        now = time.monotonic()
        # Honour Retry-After when present; otherwise back off exponentially with
        # a cap so one hostile host cannot stall the crawl indefinitely.
        pause = (
            retry_after if retry_after is not None else min(60.0, 2.0 ** min(b.throttle_events, 6))
        )
        b.blocked_until = max(b.blocked_until, now + pause)

    def on_error(self, host: str) -> None:
        """A network error is a weaker signal than a 429 -- nudge down only."""
        b = self.bucket(host)
        b.consecutive_ok = 0
        b.rate = max(b.min_rate, b.rate * 0.9)

    def snapshot(self) -> dict[str, float]:
        return {h: round(b.rate, 2) for h, b in self._buckets.items()}
