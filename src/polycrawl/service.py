"""A request/response fetch service, for putting polycrawl behind an API.

:class:`CrawlEngine` runs *one crawl*: it seeds from config, owns a frontier,
dedups permanently and finishes when that frontier drains. None of that suits a
web application, where each caller arrives with its own handful of URLs, wants
them back, and the next caller may legitimately ask for the same URL again.
Mapping a request onto an engine would also start and stop a backend per
request -- for a browser backend, a browser launch per request.

:class:`FetchService` is the other shape. One warm backend for the process,
callers hand in URLs and await their own results, and the pieces that must be
shared -- robots cache, per-host pacing -- are shared rather than rebuilt::

    service = FetchService(CrawlConfig(backend="scrapy", backend_options={"render": False}))
    await service.start()
    pages = await service.fetch(["https://example.com/a", "https://example.com/b"])
    ...
    await service.close()

Deliberately *not* a crawler: no frontier, no link following, no dedup across
callers. Depth-0 fetches of URLs someone else chose. Use ``CrawlEngine`` when you
want a crawl.

One caveat that decides deployment topology: the per-host limiter and robots
cache are per **process**. Run this in N processes behind a load balancer and a
single host sees up to N times the intended rate. See ``docs/deployment.md``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .extract import extract_page
from .models import CrawledPage, FetchRequest, FetchResult, FetchStatus
from .ratelimit import HostLimiter
from .registry import get_backend
from .robots import RobotsCache
from .urls import host_of, normalize

if TYPE_CHECKING:
    from .backend import CrawlerBackend
    from .config import CrawlConfig

log = logging.getLogger("polycrawl.service")

__all__ = ["FetchService", "ServiceBusy", "ServiceStats"]


class ServiceBusy(RuntimeError):
    """Raised when the service is already at capacity.

    Failing fast beats queueing without limit: an overloaded fetch pool that
    keeps accepting work turns a slow response into a timeout for *every*
    caller. Map this to 503 with a Retry-After.
    """


@dataclass(slots=True)
class ServiceStats:
    """Counters for a health or metrics endpoint."""

    submitted: int = 0
    completed: int = 0
    failed: int = 0
    rejected: int = 0
    robots_blocked: int = 0
    timed_out: int = 0
    retried: int = 0
    in_flight: int = 0
    started_at: float = field(default_factory=time.monotonic)

    def as_dict(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        uptime = max(1e-9, time.monotonic() - self.started_at)
        out: dict[str, Any] = {
            "submitted": self.submitted,
            "completed": self.completed,
            "failed": self.failed,
            "rejected": self.rejected,
            "robots_blocked": self.robots_blocked,
            "timed_out": self.timed_out,
            "retried": self.retried,
            "in_flight": self.in_flight,
            "uptime_s": round(uptime, 1),
            "completed_per_second": round(self.completed / uptime, 2),
        }
        if extra:
            out.update(extra)
        return out


class FetchService:
    """A warm fetch pool with politeness, sized for concurrent callers.

    Args:
        config: Backend selection and every limit. ``concurrency`` caps in-flight
            fetches for the whole process, and is what protects the box.
        request_timeout: Per-URL ceiling, seconds. A caller gets a timed-out
            page rather than waiting on a host that has stopped answering.
        queue_timeout: How long a URL may wait for a free slot before
            :class:`ServiceBusy`. 0 rejects immediately when full.
        max_retries: Retries for transient failures only, per URL. ``None``
            takes the value from ``config.max_retries``.
    """

    def __init__(
        self,
        config: CrawlConfig,
        *,
        request_timeout: float = 30.0,
        queue_timeout: float = 5.0,
        max_retries: int | None = None,
        backend: CrawlerBackend | None = None,
    ) -> None:
        self.config = config
        self.request_timeout = request_timeout
        self.queue_timeout = queue_timeout
        self.max_retries = config.max_retries if max_retries is None else max_retries
        self.stats = ServiceStats()

        self._backend = backend
        self._owns_backend = backend is None
        self._started = False
        self._closed = False

        # Shared, not per caller: the whole point of one service per process.
        self.limiter = HostLimiter(
            base_rate=config.politeness.per_host_rps,
            burst=config.politeness.per_host_burst,
            min_rate=config.politeness.min_host_rps,
            max_rate=config.politeness.max_host_rps,
        )
        self.robots = RobotsCache(
            user_agent=config.politeness.user_agent_token,
            timeout=config.politeness.robots_timeout_s,
        )

        self._slots = asyncio.Semaphore(config.concurrency)
        self._host_slots: dict[str, asyncio.Semaphore] = {}
        self._waiters: dict[int, asyncio.Future[FetchResult]] = {}
        self._next_id = 0

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        """Bring the backend up. Call once, from your app's startup hook."""
        if self._started:
            return
        if self._backend is None:
            backend_cls = get_backend(self.config.backend)
            backend_cls.require()
            self._backend = backend_cls(self.config)
        await self._backend.start(self._on_result)
        self._started = True
        log.info(
            "fetch service ready (backend=%s, concurrency=%d)",
            self.config.backend,
            self.config.concurrency,
        )

    async def close(self) -> None:
        """Release the backend. In-flight callers get a cancellation."""
        self._closed = True
        for future in list(self._waiters.values()):
            if not future.done():
                future.cancel()
        self._waiters.clear()
        if self._owns_backend and self._backend is not None:
            await self._backend.close()
        self._started = False

    async def __aenter__(self) -> FetchService:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def ready(self) -> bool:
        return self._started and not self._closed

    # -- public API -------------------------------------------------------

    async def fetch(self, urls: Sequence[str]) -> list[CrawledPage]:
        """Fetch several URLs concurrently and return a page for each.

        Results come back in the order asked for. Every URL yields a page, even
        when it failed -- a caller wants to know *which* of its URLs failed and
        why, not to lose the batch to one bad host. Raises
        :class:`ServiceBusy` only when no slot came free in time.
        """
        if not self.ready:
            raise RuntimeError("FetchService.start() was not called")
        if not urls:
            return []

        results = await asyncio.gather(*(self._fetch_one(u) for u in urls))
        return list(results)

    async def fetch_one(self, url: str) -> CrawledPage:
        pages = await self.fetch([url])
        return pages[0]

    def snapshot(self) -> dict[str, Any]:
        return self.stats.as_dict(
            {
                "backend": self.config.backend,
                "concurrency": self.config.concurrency,
                "hosts_tracked": len(self.limiter.snapshot()),
                "robots": self.robots.stats(),
            }
        )

    # -- one URL ----------------------------------------------------------

    async def _fetch_one(self, raw_url: str) -> CrawledPage:
        url = normalize(raw_url, strip_query=self.config.strip_query)
        if not url:
            return _error_page(raw_url, "unusable url")

        if self.config.politeness.respect_robots:
            allowed, delay = await self._robots_allows(url)
            if delay:
                self.limiter.set_crawl_delay(host_of(url), delay)
            if not allowed:
                self.stats.robots_blocked += 1
                return _error_page(url, "blocked by robots.txt", FetchStatus.BLOCKED_ROBOTS)

        try:
            await asyncio.wait_for(self._slots.acquire(), timeout=self.queue_timeout or 0.001)
        except TimeoutError:
            self.stats.rejected += 1
            raise ServiceBusy(
                f"fetch service at capacity ({self.config.concurrency} in flight)"
            ) from None

        try:
            return await self._attempt_with_retries(url)
        finally:
            self._slots.release()

    async def _robots_allows(self, url: str) -> tuple[bool, float | None]:
        try:
            return await self.robots.can_fetch(url)
        except Exception as exc:
            # Same policy as the engine: a robots lookup that fails must not
            # take the fetch down with it.
            log.debug("robots lookup failed for %s: %s", url, exc)
            return True, None

    async def _attempt_with_retries(self, url: str) -> CrawledPage:
        host = host_of(url)
        attempt = 0
        while True:
            result = await self._attempt(url, host, attempt)
            if result.is_success or not result.status.is_retryable or attempt >= self.max_retries:
                break
            attempt += 1
            self.stats.retried += 1
            await asyncio.sleep(self.config.retry_backoff_s * (2 ** (attempt - 1)))

        if result.is_success:
            self.stats.completed += 1
        else:
            self.stats.failed += 1
            if result.status is FetchStatus.TIMEOUT:
                self.stats.timed_out += 1
        return self._to_page(result)

    async def _attempt(self, url: str, host: str, attempt: int) -> FetchResult:
        async with self._host_slot(host):
            await self._await_host_token(host)

            request = FetchRequest(url=url, depth=0, attempt=attempt)
            loop = asyncio.get_running_loop()
            self._next_id += 1
            rid = self._next_id
            future: asyncio.Future[FetchResult] = loop.create_future()
            self._waiters[rid] = future
            # The id travels with the request and comes back on the result, which
            # is how a fetch finds the caller that asked for it.
            request.meta["polycrawl_rid"] = rid

            self.stats.submitted += 1
            self.stats.in_flight += 1
            try:
                assert self._backend is not None
                await self._backend.submit([request])
                result = await asyncio.wait_for(future, timeout=self.request_timeout)
            except TimeoutError:
                return FetchResult(
                    request=request,
                    status=FetchStatus.TIMEOUT,
                    url=url,
                    error=f"no response within {self.request_timeout}s",
                )
            except Exception as exc:
                return FetchResult(
                    request=request,
                    status=FetchStatus.BACKEND_ERROR,
                    url=url,
                    error=f"{type(exc).__name__}: {exc}",
                )
            finally:
                self.stats.in_flight -= 1
                self._waiters.pop(rid, None)

            self._feed_limiter(host, result)
            return result

    def _host_slot(self, host: str) -> asyncio.Semaphore:
        """Cap simultaneous fetches per host, not just globally."""
        slot = self._host_slots.get(host)
        if slot is None:
            slot = asyncio.Semaphore(self.config.politeness.per_host_concurrency)
            self._host_slots[host] = slot
        return slot

    async def _await_host_token(self, host: str) -> None:
        """Block until this host's token bucket allows another request."""
        while not self.limiter.try_acquire(host):
            wait = self.limiter.ready_at(host) - time.monotonic()
            await asyncio.sleep(min(max(wait, 0.005), 1.0))

    def _feed_limiter(self, host: str, result: FetchResult) -> None:
        if result.http_status in (429, 503):
            self.limiter.on_throttled(host, _retry_after(result.headers))
        elif result.is_success:
            if self.config.politeness.adaptive:
                self.limiter.on_success(host)
        elif result.status.is_retryable:
            self.limiter.on_error(host)

    async def _on_result(self, result: FetchResult) -> None:
        """Backend callback: hand the result to whoever is waiting for it."""
        rid = result.request.meta.get("polycrawl_rid")
        future = self._waiters.pop(rid, None) if rid is not None else None
        if future is None:
            # The caller timed out or went away; nothing to deliver to.
            log.debug("no waiter for %s", result.url)
            return
        if not future.done():
            future.set_result(result)

    def _to_page(self, result: FetchResult) -> CrawledPage:
        out = self.config.output
        text = (result.text or "") if out.text_source == "backend" else ""
        extract = extract_page(
            result.html,
            result.url,
            want_links=out.include_links,
            want_text=out.include_text and not text,
            strip_query=self.config.strip_query,
            max_text_chars=out.max_text_chars,
        )
        return CrawledPage(
            url=result.request.url,
            final_url=result.url,
            depth=0,
            parent=None,
            http_status=result.http_status,
            status=result.status.value,
            title=extract.title,
            text=(text or extract.text) if out.include_text else "",
            html=result.html if out.include_html else None,
            links=extract.links if out.include_links else [],
            n_links=len(extract.links),
            error=result.error,
            elapsed=result.elapsed,
            backend=result.backend,
            fetched_at=result.fetched_at,
        )


def _error_page(url: str, error: str, status: FetchStatus = FetchStatus.SKIPPED) -> CrawledPage:
    return CrawledPage(
        url=url,
        final_url=url,
        depth=0,
        parent=None,
        http_status=None,
        status=status.value,
        title="",
        text="",
        html=None,
        links=[],
        n_links=0,
        error=error,
        elapsed=0.0,
        backend="",
        fetched_at=time.time(),
    )


def _retry_after(headers: dict[str, str]) -> float | None:
    raw = (headers or {}).get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw.strip()))
    except ValueError:
        return None
