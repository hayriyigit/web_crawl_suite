"""The URL frontier.

Ordering is the difference between a crawler that saturates its browser pool
and one that idles. A single global FIFO queue tends to hold long runs of URLs
from the same host (pages link to their own site), so a politeness limiter of
a few requests/second per host throttles the *entire* crawl down to that rate
no matter how much concurrency is configured.

This frontier keeps one queue per host and hands out work round-robin across
hosts, so a batch of N URLs spreads over as many distinct hosts as are
available. Hosts that are rate-limited or already at their in-flight cap are
skipped without losing their place.
"""

from __future__ import annotations

import heapq
import time
from collections import deque
from collections.abc import Callable, Iterable

from .models import FetchRequest

__all__ = ["Frontier"]


class Frontier:
    """Depth-ordered, host-fair request queue with a bounded footprint."""

    __slots__ = (
        "_deferred",
        "_dropped",
        "_hosts",
        "_in_ring",
        "_max_per_host",
        "_ring",
        "_size",
        "max_size",
    )

    def __init__(self, max_size: int = 1_000_000, max_per_host: int = 100_000) -> None:
        self._hosts: dict[str, deque[FetchRequest]] = {}
        #: Round-robin ring of hosts that currently hold work.
        self._ring: deque[str] = deque()
        self._in_ring: set[str] = set()
        #: Min-heap of (ready_at, host) for hosts skipped on rate-limit grounds.
        self._deferred: list[tuple[float, str]] = []
        self._size = 0
        self._dropped = 0
        self.max_size = max_size
        self._max_per_host = max_per_host

    def __len__(self) -> int:
        return self._size

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def n_hosts(self) -> int:
        return len(self._hosts)

    def push(self, request: FetchRequest, *, front: bool = False) -> bool:
        """Enqueue a request. Returns ``False`` if a bound rejected it.

        ``front`` puts the request at the head of its host queue -- used for
        retries so a transient failure is re-tried promptly rather than after
        the whole frontier drains.
        """
        if self._size >= self.max_size:
            self._dropped += 1
            return False
        host = request.host
        q = self._hosts.get(host)
        if q is None:
            q = self._hosts[host] = deque()
        elif len(q) >= self._max_per_host:
            # One runaway host (calendar pages, faceted search) must not be
            # allowed to consume the whole frontier budget.
            self._dropped += 1
            return False
        if front:
            q.appendleft(request)
        else:
            q.append(request)
        self._size += 1
        if host not in self._in_ring:
            self._in_ring.add(host)
            self._ring.append(host)
        return True

    def push_many(self, requests: Iterable[FetchRequest]) -> int:
        return sum(1 for r in requests if self.push(r))

    def pop_batch(
        self,
        limit: int,
        *,
        is_ready: Callable[[str], bool] | None = None,
        ready_at: Callable[[str], float] | None = None,
        per_host: int = 1,
    ) -> list[FetchRequest]:
        """Take up to ``limit`` requests, spread across as many hosts as possible.

        ``is_ready(host)`` gates a host on rate limit / in-flight budget. When
        it returns ``False`` the host is parked in the deferred heap keyed by
        ``ready_at(host)`` instead of being spun over repeatedly, which keeps
        this loop O(ready hosts) rather than O(all hosts) when most of the
        frontier is throttled.
        """
        if limit <= 0 or self._size == 0:
            return []

        self._revive_deferred()

        batch: list[FetchRequest] = []
        ring = self._ring
        # Each host is visited at most once per call; without this bound a
        # frontier where every host is throttled would spin forever.
        rotations = len(ring)

        while ring and len(batch) < limit and rotations > 0:
            host = ring[0]
            rotations -= 1
            q = self._hosts.get(host)

            if not q:
                ring.popleft()
                self._in_ring.discard(host)
                self._hosts.pop(host, None)
                continue

            if is_ready is not None and not is_ready(host):
                ring.popleft()
                self._in_ring.discard(host)
                when = ready_at(host) if ready_at is not None else time.monotonic() + 0.05
                heapq.heappush(self._deferred, (when, host))
                continue

            taken = 0
            while q and taken < per_host and len(batch) < limit:
                batch.append(q.popleft())
                self._size -= 1
                taken += 1

            # Rotate so the next call starts at a different host.
            ring.rotate(-1)

        return batch

    def _revive_deferred(self) -> None:
        """Return hosts whose cooldown has elapsed to the round-robin ring."""
        if not self._deferred:
            return
        now = time.monotonic()
        heap = self._deferred
        while heap and heap[0][0] <= now:
            _, host = heapq.heappop(heap)
            if host in self._in_ring:
                continue
            if self._hosts.get(host):
                self._in_ring.add(host)
                self._ring.append(host)

    def next_ready_time(self) -> float | None:
        """Earliest monotonic time any deferred host becomes available."""
        return self._deferred[0][0] if self._deferred else None

    def has_work(self) -> bool:
        return self._size > 0

    def stats(self) -> dict[str, int]:
        return {
            "queued": self._size,
            "hosts": len(self._hosts),
            "ready_hosts": len(self._ring),
            "deferred_hosts": len(self._deferred),
            "dropped": self._dropped,
        }
