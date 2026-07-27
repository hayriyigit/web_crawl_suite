"""The crawl engine.

Owns everything that is not "how do I fetch a URL": seeding, scope, dedup,
robots, per-host politeness, retries, link extraction and output. Backends see
only :class:`FetchRequest` in and :class:`FetchResult` out, which is what makes
them swappable without changing crawl behaviour.

Shape of the pipeline::

    frontier ──► scheduler ──► backend.submit()  (bounded by `concurrency`)
                    ▲                │
                    │                ▼  backend.emit()
              enqueue links   result queue (bounded → backpressure)
                    │                │
                    └──── processor workers ────► sink

Two separate concurrency budgets: `concurrency` caps in-flight *fetches*, and
`processor_workers` caps concurrent *parsing*. Keeping them apart means a burst
of large pages slows parsing without also throttling the network, and a bounded
result queue applies backpressure to the backend when parsing falls behind.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import Counter
from collections.abc import Sequence

from .backend import CrawlerBackend
from .config import CrawlConfig
from .dedup import make_seen_set
from .extract import extract_page
from .frontier import Frontier
from .memory import memory_used_pct
from .metrics import Metrics
from .models import CrawledPage, FetchRequest, FetchResult
from .ratelimit import HostLimiter
from .registry import get_backend
from .robots import RobotsCache
from .sinks import Sink, make_sink
from .urls import ScopeRules, host_of, normalize

log = logging.getLogger("polycrawl.engine")

__all__ = ["CrawlEngine", "crawl"]

#: How long the scheduler sleeps when it has nothing to dispatch. Short enough
#: to stay responsive, long enough not to spin a core.
_IDLE_TICK = 0.02
_MAX_IDLE_TICK = 0.25


class CrawlEngine:
    """Runs one crawl to completion."""

    def __init__(
        self,
        config: CrawlConfig,
        backend: CrawlerBackend | None = None,
        sink: Sink | None = None,
    ) -> None:
        self.config = config
        self.metrics = Metrics()

        self.frontier = Frontier(
            max_size=config.max_frontier,
            max_per_host=config.max_frontier_per_host,
        )
        self.seen = make_seen_set(
            config.dedup,
            capacity=config.dedup_capacity,
            error_rate=config.dedup_error_rate,
        )
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
        self.scope = ScopeRules(
            config.seeds,
            mode=config.scope,
            allow_patterns=config.allow_patterns,
            deny_patterns=config.deny_patterns,
            allow_domains=config.allow_domains,
            deny_domains=config.deny_domains,
            max_depth=config.max_depth,
            skip_binary=config.skip_binary,
        )

        self._owns_backend = backend is None
        self._backend = backend
        self._owns_sink = sink is None
        self._sink = sink

        # A bounded queue is the backpressure mechanism: when parsing falls
        # behind, backend.emit() blocks and fetching naturally slows down.
        self._results: asyncio.Queue[FetchResult | None] = asyncio.Queue(
            maxsize=max(32, config.concurrency * 4)
        )
        self._in_flight = 0
        self._host_in_flight: Counter[str] = Counter()
        self._dispatch_sem = asyncio.Semaphore(config.pipeline_depth)
        self._dispatch_tasks: set[asyncio.Task[None]] = set()
        self._retry_tasks: set[asyncio.Task[None]] = set()
        #: Remaining pages we are allowed to fetch, reserved at dispatch time.
        self._budget = config.max_pages
        self._wake = asyncio.Event()
        self._stopping = False
        self._paused_for_memory = False

    # -- public API -------------------------------------------------------

    @property
    def backend(self) -> CrawlerBackend:
        if self._backend is None:
            raise RuntimeError("engine backend not initialised; call run()")
        return self._backend

    @property
    def sink(self) -> Sink:
        if self._sink is None:
            raise RuntimeError("engine sink not initialised; call run()")
        return self._sink

    async def run(self) -> Metrics:
        """Execute the crawl and return its metrics."""
        cfg = self.config
        if not cfg.seeds:
            raise ValueError("no seed URLs configured")

        if self._backend is None:
            backend_cls = get_backend(cfg.backend)
            backend_cls.require()
            self._backend = backend_cls(cfg)
        if self._sink is None:
            self._sink = make_sink(
                cfg.output.path,
                cfg.output.format,
                include_html=cfg.output.include_html,
                buffer_bytes=cfg.output.buffer_bytes,
            )

        self._seed()

        workers: list[asyncio.Task[None]] = []
        await self._sink.open()
        try:
            await self._backend.start(self._on_result)
            workers = [
                asyncio.create_task(self._processor(i), name=f"processor-{i}")
                for i in range(cfg.processor_workers)
            ]
            await self._schedule_loop()
            await self._quiesce()
        finally:
            # Signal every processor to exit, then let them finish the queue.
            for _ in workers:
                self._results.put_nowait(None)
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)
            await self._cancel_pending()
            if self._owns_backend and self._backend is not None:
                with contextlib.suppress(Exception):
                    await self._backend.close()
            if self._owns_sink and self._sink is not None:
                with contextlib.suppress(Exception):
                    await self._sink.close()
            self.metrics.finish()

        return self.metrics

    def stop(self, reason: str = "requested") -> None:
        """Ask the crawl to wind down; in-flight work still completes."""
        if not self._stopping:
            log.info("stopping crawl (%s)", reason)
        self._stopping = True
        self._wake.set()

    # -- seeding ----------------------------------------------------------

    def _seed(self) -> None:
        for raw in self.config.seeds:
            url = normalize(raw, strip_query=self.config.strip_query)
            if not url:
                log.warning("skipping unusable seed: %s", raw)
                continue
            if self.seen.add(url):
                self.frontier.push(FetchRequest(url=url, depth=0))
                self.metrics.enqueued += 1
        if not len(self.frontier):
            raise ValueError("no usable seed URLs after normalisation")

    # -- scheduling -------------------------------------------------------

    async def _schedule_loop(self) -> None:
        cfg = self.config
        deadline = self.metrics.started_at + cfg.time_budget_s if cfg.time_budget_s else None
        idle = _IDLE_TICK

        while True:
            if self._stopping:
                break
            if deadline is not None and time.monotonic() >= deadline:
                log.info("time budget of %.0fs reached", cfg.time_budget_s)
                break
            if self._budget <= 0 and self._in_flight == 0 and not self._dispatch_tasks:
                break
            if self._is_finished():
                break

            batch = self._next_batch()
            if batch:
                idle = _IDLE_TICK
                await self._dispatch_sem.acquire()
                task = asyncio.create_task(self._dispatch(batch))
                self._dispatch_tasks.add(task)
                task.add_done_callback(self._on_dispatch_done)
                continue

            # Nothing dispatchable: wait for a result, a retry, or a host's
            # rate-limit cooldown, whichever comes first.
            timeout = idle
            next_ready = self.frontier.next_ready_time()
            if next_ready is not None:
                timeout = max(_IDLE_TICK, min(timeout, next_ready - time.monotonic()))
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            # Back off gently while idle so a long tail does not burn CPU.
            idle = min(_MAX_IDLE_TICK, idle * 1.5)

    def _is_finished(self) -> bool:
        return (
            not self.frontier.has_work()
            and self._in_flight == 0
            and not self._dispatch_tasks
            and not self._retry_tasks
            and self._results.empty()
        )

    def _next_batch(self) -> list[FetchRequest]:
        """Reserve the next slice of work, respecting every global bound."""
        cfg = self.config
        if self._budget <= 0 or self._stopping:
            return []
        if self._dispatch_sem.locked():
            return []
        if self._under_memory_pressure():
            return []

        available = cfg.concurrency - self._in_flight
        limit = min(available, cfg.batch_size, self._budget)
        if limit <= 0:
            return []

        batch = self.frontier.pop_batch(
            limit,
            is_ready=self._host_ready,
            ready_at=self.limiter.ready_at,
            per_host=1,
        )
        if not batch:
            return []

        # Reserve capacity now so concurrent dispatches cannot oversubscribe.
        self._in_flight += len(batch)
        self._budget -= len(batch)
        for req in batch:
            self._host_in_flight[host_of(req.url)] += 1
        return batch

    def _host_ready(self, host: str) -> bool:
        """A host is dispatchable when it is under its in-flight cap and has a token."""
        if self._host_in_flight[host] >= self.config.politeness.per_host_concurrency:
            return False
        return self.limiter.try_acquire(host)

    def _under_memory_pressure(self) -> bool:
        used = memory_used_pct()
        if used is None:
            return False
        limit = self.config.memory_limit_pct
        if used >= limit:
            if not self._paused_for_memory:
                log.warning("memory at %.1f%% (limit %.1f%%): pausing dispatch", used, limit)
                self._paused_for_memory = True
            return True
        if self._paused_for_memory and used < limit - 5.0:
            log.info("memory recovered to %.1f%%: resuming dispatch", used)
            self._paused_for_memory = False
        return self._paused_for_memory

    async def _dispatch(self, batch: Sequence[FetchRequest]) -> None:
        """Filter a reserved batch through robots.txt, then hand it to the backend."""
        allowed: list[FetchRequest] = list(batch)
        try:
            if self.config.politeness.respect_robots:
                allowed = await self._filter_robots(batch)
            if allowed:
                await self.backend.submit(allowed)
        except asyncio.CancelledError:
            for req in allowed:
                self._release(req)
            raise
        except Exception as exc:
            log.error("backend submit failed for %d urls: %s", len(allowed), exc)
            self.metrics.record_error(f"submit:{type(exc).__name__}")
            # The backend will never emit these, so account for them here.
            for req in allowed:
                self.metrics.record_fetch(ok=False, http_status=None, elapsed=0.0)
                self._release(req)
            self._wake.set()

    async def _filter_robots(self, batch: Sequence[FetchRequest]) -> list[FetchRequest]:
        checks = await asyncio.gather(
            *(self.robots.can_fetch(req.url) for req in batch),
            return_exceptions=True,
        )
        allowed: list[FetchRequest] = []
        for req, outcome in zip(batch, checks, strict=True):
            if isinstance(outcome, BaseException):
                # Robots lookup failed -> fail open, same as a 5xx robots.txt.
                allowed.append(req)
                continue
            ok, delay = outcome
            if delay:
                self.limiter.set_crawl_delay(host_of(req.url), delay)
            if ok:
                allowed.append(req)
            else:
                self.metrics.robots_blocked += 1
                self._release(req)
                # A blocked URL was never fetched: give the budget back.
                self._budget += 1
        if len(allowed) != len(batch):
            self._wake.set()
        return allowed

    def _on_dispatch_done(self, task: asyncio.Task[None]) -> None:
        self._dispatch_tasks.discard(task)
        self._dispatch_sem.release()
        if not task.cancelled() and task.exception() is not None:
            log.error("dispatch task failed: %r", task.exception())
        self._wake.set()

    def _release(self, req: FetchRequest) -> None:
        """Return a request's reserved fetch slot.

        Tolerates a double release: a backend that emits twice for one request
        must not be able to wedge the accounting (or raise, which would strand
        the rest of its batch and leave in-flight permanently above zero).
        """
        self._in_flight = max(0, self._in_flight - 1)
        host = host_of(req.url)
        remaining = self._host_in_flight.get(host, 0) - 1
        if remaining > 0:
            self._host_in_flight[host] = remaining
        else:
            # pop, not del: Counter[missing] returns 0 rather than raising, so
            # `del` on an absent host would be a KeyError.
            self._host_in_flight.pop(host, None)

    # -- result intake ----------------------------------------------------

    async def _on_result(self, result: FetchResult) -> None:
        """Backend callback. Frees the fetch slot, then queues for parsing."""
        self._release(result.request)
        self._wake.set()
        await self._results.put(result)

    async def _processor(self, worker_id: int) -> None:
        """Parse fetched pages, enqueue their links and write them out."""
        while True:
            item = await self._results.get()
            try:
                if item is None:
                    return
                await self._handle_result(item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("processor %d failed on %s: %s", worker_id, item.url, exc)
                self.metrics.record_error(f"process:{type(exc).__name__}")
            finally:
                self._results.task_done()
                self._wake.set()

    async def _handle_result(self, result: FetchResult) -> None:
        cfg = self.config
        host = host_of(result.url or result.request.url)

        # Feed the adaptive limiter before anything else can fail.
        if result.http_status in (429, 503):
            retry_after = _parse_retry_after(result.headers)
            self.limiter.on_throttled(host, retry_after)
        elif result.is_success:
            if cfg.politeness.adaptive:
                self.limiter.on_success(host)
        elif result.status.is_retryable:
            self.limiter.on_error(host)

        if not result.is_success and result.status.is_retryable:
            if await self._maybe_retry(result):
                return

        self.metrics.record_fetch(
            ok=result.is_success,
            http_status=result.http_status,
            elapsed=result.elapsed,
            nbytes=len(result.html),
        )
        if not result.is_success:
            self.metrics.record_error(result.status.value)

        title = ""
        # A backend's own text is used only when asked for. Otherwise extraction
        # is central and every backend returns the same field for the same HTML,
        # which is what lets one be swapped for another.
        text = (result.text or "") if cfg.output.text_source == "backend" else ""
        links: list[str] = []

        if result.html:
            want_links = cfg.follow_links and result.request.depth < cfg.max_depth
            extract = extract_page(
                result.html,
                result.url,
                want_links=want_links and result.links is None,
                want_text=cfg.output.include_text and not text,
                strip_query=cfg.strip_query,
                max_text_chars=cfg.output.max_text_chars,
            )
            title = extract.title
            if not text:
                text = extract.text
            # A backend that already found links saves us the second pass.
            links = result.links if result.links is not None else extract.links
            if want_links and links:
                self._enqueue_links(links, result)

        page = CrawledPage(
            url=result.request.url,
            final_url=result.url,
            depth=result.request.depth,
            parent=result.request.parent,
            http_status=result.http_status,
            status=result.status.value,
            title=title,
            text=text if cfg.output.include_text else "",
            html=result.html if cfg.output.include_html else None,
            links=links if cfg.output.include_links else [],
            n_links=len(links),
            error=result.error,
            elapsed=result.elapsed,
            backend=result.backend,
            fetched_at=result.fetched_at,
        )
        await self.sink.write(page)

    def _enqueue_links(self, links: Sequence[str], result: FetchResult) -> None:
        depth = result.request.depth + 1
        parent = result.url
        metrics = self.metrics
        scope = self.scope
        seen = self.seen
        frontier = self.frontier
        strip_query = self.config.strip_query

        for link in links:
            metrics.discovered += 1
            # Backend-supplied links have not been through our normaliser.
            url = link if result.links is None else normalize(link, strip_query=strip_query)
            if not url:
                continue
            if not scope.allows(url, depth):
                metrics.out_of_scope += 1
                continue
            if not seen.add(url):
                metrics.duplicates += 1
                continue
            if frontier.push(FetchRequest(url=url, depth=depth, parent=parent)):
                metrics.enqueued += 1
        self._wake.set()

    async def _maybe_retry(self, result: FetchResult) -> bool:
        """Re-queue a transient failure after a backoff. True if re-queued."""
        req = result.request
        if req.attempt >= self.config.max_retries:
            return False
        delay = self.config.retry_backoff_s * (2**req.attempt)
        retry = FetchRequest(
            url=req.url,
            depth=req.depth,
            parent=req.parent,
            attempt=req.attempt + 1,
            meta=req.meta,
        )
        self.metrics.retried += 1
        # The budget was consumed by the failed attempt; a retry is the same
        # logical page, so hand the slot back rather than counting it twice.
        self._budget += 1
        task = asyncio.create_task(self._requeue_after(retry, delay))
        self._retry_tasks.add(task)
        task.add_done_callback(self._retry_tasks.discard)
        return True

    async def _requeue_after(self, req: FetchRequest, delay: float) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            self.frontier.push(req, front=True)
        except asyncio.CancelledError:
            raise
        finally:
            self._wake.set()

    # -- shutdown ---------------------------------------------------------

    async def _quiesce(self) -> None:
        """Let outstanding fetches and parses finish before tearing down."""
        if self._dispatch_tasks:
            await asyncio.gather(*list(self._dispatch_tasks), return_exceptions=True)
        with contextlib.suppress(Exception):
            await self.backend.drain()
        # drain() guarantees every submitted request was emitted; the queue may
        # still hold the last of them.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._results.join(), timeout=120)

    async def _cancel_pending(self) -> None:
        pending = list(self._retry_tasks) + list(self._dispatch_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    # -- reporting --------------------------------------------------------

    def snapshot(self) -> dict[str, object]:
        """Live view for progress display."""
        return {
            **self.metrics.summary(),
            **self.frontier.stats(),
            "in_flight": self._in_flight,
            "budget_left": self._budget,
            "seen": len(self.seen),
            "recent_pps": round(self.metrics.recent_pages_per_second, 2),
            "paused_memory": self._paused_for_memory,
        }


def _parse_retry_after(headers: dict[str, str]) -> float | None:
    """Seconds from a ``Retry-After`` header, when it is a delta-seconds value."""
    if not headers:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw.strip()))
    except ValueError:
        # HTTP-date form: fall back to the caller's default backoff.
        return None


async def crawl(config: CrawlConfig, **kwargs: object) -> Metrics:
    """Convenience wrapper: build an engine, run it, return metrics."""
    engine = CrawlEngine(config, **kwargs)  # type: ignore[arg-type]
    return await engine.run()
