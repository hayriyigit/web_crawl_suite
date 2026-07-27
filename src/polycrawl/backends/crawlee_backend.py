"""Crawlee backend.

Crawlee's ``PlaywrightCrawler`` is designed to own its whole crawl -- request
queue, retries, autoscaling -- but here the engine owns those. The bridge is
``keep_alive=True``: the crawler is started once with an empty queue and left
running, and :meth:`submit` pushes requests into its live queue. That keeps a
single browser pool warm for the entire crawl instead of paying a browser
launch per batch, which is the difference between ~1s and ~50ms of overhead per
batch.

Crawlee's own retry and robots handling are switched off so the engine remains
the single authority on what gets fetched and when.

Every instance also runs on its own crawlee services rather than the library's
process-global ones -- see :func:`_isolated_storage_client` for why that is not
optional.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import uuid4

from ..backend import BackendCapabilities, CrawlerBackend, EmitFn
from ..models import FetchRequest, FetchResult, FetchStatus

if TYPE_CHECKING:
    from ..config import CrawlConfig

log = logging.getLogger("polycrawl.backends.crawlee")

__all__ = ["CrawleeBackend"]

_TIMEOUT_MARKERS = ("timeout", "timed out")
_NETWORK_MARKERS = ("net::err", "connection", "dns", "ssl", "econnrefused", "unreachable")


class CrawleeBackend(CrawlerBackend):
    """Browser-rendered fetching via Crawlee's PlaywrightCrawler."""

    name: ClassVar[str] = "crawlee"
    install_extra: ClassVar[str] = "crawlee"
    capabilities: ClassVar[BackendCapabilities] = BackendCapabilities(
        javascript=True,
        native_links=False,
        native_markdown=False,
        proxy=True,
        stealth=True,
        screenshots=True,
    )

    def __init__(self, config: CrawlConfig) -> None:
        super().__init__(config)
        self._crawler: Any = None
        #: Storages opened for this instance only, dropped in close().
        self._request_queue: Any = None
        self._run_task: asyncio.Task[Any] | None = None
        self._pending = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._closed = False
        #: key -> (our request, submit time). Crawlee only round-trips
        #: ``user_data``, so this is how depth/attempt/parent come back. The
        #: timestamp is taken at submit because crawlee navigates *before*
        #: invoking the handler -- timing from inside the handler would measure
        #: only ``page.content()`` and report latencies near zero.
        self._inflight: dict[str, tuple[FetchRequest, float]] = {}

    @classmethod
    def is_available(cls) -> tuple[bool, str]:
        try:
            import crawlee  # noqa: F401
            from crawlee.crawlers import PlaywrightCrawler  # noqa: F401
        except ImportError as exc:
            return False, f"crawlee[playwright] is not installed ({exc})"
        return True, ""

    # -- lifecycle --------------------------------------------------------

    async def start(self, emit: EmitFn) -> None:
        from crawlee import ConcurrencySettings
        from crawlee.configuration import Configuration
        from crawlee.crawlers import PlaywrightCrawler
        from crawlee.events import LocalEventManager
        from crawlee.storages import RequestQueue

        self._emit = emit
        cfg = self.config
        b = cfg.browser

        # Private services for this crawl. Passing all three makes crawlee build
        # the crawler its own ServiceLocator instead of reaching for the global
        # one; the storage client is what actually keeps successive crawls apart.
        configuration = Configuration(**dict(cfg.backend_options.get("configuration", {})))
        storage_client = _isolated_storage_client()
        self._request_queue = await RequestQueue.open(
            alias=f"polycrawl-{uuid4().hex}",
            configuration=configuration,
            storage_client=storage_client,
        )

        concurrency = ConcurrencySettings(
            min_concurrency=1,
            desired_concurrency=max(1, min(cfg.concurrency, cfg.batch_size)),
            max_concurrency=max(1, cfg.concurrency),
        )

        launch_options: dict[str, Any] = {}
        args = list(b.extra_args) + _blocking_args(b)
        if args:
            launch_options["args"] = args

        context_options: dict[str, Any] = {
            "viewport": {"width": b.viewport_width, "height": b.viewport_height},
            "ignore_https_errors": b.ignore_https_errors,
        }
        if b.user_agent:
            context_options["user_agent"] = b.user_agent

        self._crawler = PlaywrightCrawler(
            headless=b.headless,
            browser_type=b.browser_type,
            browser_launch_options=launch_options or None,
            browser_new_context_options=context_options,
            # The DOM-ready contract, applied to every navigation.
            goto_options={"wait_until": b.wait_until},
            navigation_timeout=timedelta(milliseconds=b.page_timeout_ms),
            concurrency_settings=concurrency,
            # Per-instance services; see the comment above the queue.
            configuration=configuration,
            storage_client=storage_client,
            event_manager=LocalEventManager(),
            request_manager=self._request_queue,
            # The engine owns retries, robots and page budgets.
            max_request_retries=0,
            retry_on_blocked=False,
            respect_robots_txt_file=False,
            # The session pool would resolve its persisted state through the
            # *global* service locator, reintroducing the shared state the rest
            # of this wiring avoids. The engine owns blocking policy anyway, and
            # cookies live in the shared browser context either way.
            use_session_pool=False,
            request_handler_timeout=timedelta(milliseconds=b.page_timeout_ms + 15_000),
            # Non-2xx responses must reach our handler rather than being
            # turned into crawlee-level failures.
            ignore_http_error_status_codes=list(range(400, 600)),
            # Stay running with an empty queue so submit() can feed it.
            keep_alive=True,
            configure_logging=False,
            statistics_log_format="inline",
            **dict(cfg.backend_options.get("crawler", {})),
        )

        self._register_handlers()
        self._run_task = asyncio.create_task(self._run_forever(), name="crawlee-run")
        await self._await_ready()
        log.debug("crawlee backend started (concurrency=%d)", cfg.concurrency)

    async def _await_ready(self) -> None:
        """Give the crawler a moment to come up, and surface immediate failures."""
        for _ in range(100):
            if self._run_task is not None and self._run_task.done():
                exc = self._run_task.exception()
                if exc is not None:
                    raise RuntimeError(f"crawlee failed to start: {exc}") from exc
                return
            if getattr(self._crawler, "_running", False):
                return
            await asyncio.sleep(0.05)

    async def _run_forever(self) -> None:
        try:
            await self._crawler.run([])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("crawlee run loop terminated")
            raise

    def _register_handlers(self) -> None:
        crawler = self._crawler

        @crawler.router.default_handler
        async def _handle(context: Any) -> None:
            await self._on_page(context)

        @crawler.failed_request_handler
        async def _failed(context: Any, error: Exception) -> None:
            await self._on_failure(context, error)

    # -- submission -------------------------------------------------------

    async def submit(self, batch: Sequence[FetchRequest]) -> None:
        from crawlee import Request

        if self._closed:
            raise RuntimeError("backend is closed")
        if not batch:
            return
        if self._run_task is not None and self._run_task.done():
            raise RuntimeError("crawlee run loop is not active")

        requests = []
        now = time.monotonic()
        for req in batch:
            key = f"{id(req):x}-{req.attempt}-{time.monotonic_ns()}"
            self._inflight[key] = (req, now)
            requests.append(
                Request.from_url(
                    req.url,
                    user_data={"polycrawl_key": key},
                    # The engine already dedups; crawlee's unique-key filter
                    # would otherwise silently drop legitimate re-fetches.
                    always_enqueue=True,
                )
            )

        self._pending += len(requests)
        self._idle.clear()
        await self._crawler.add_requests(requests)

    async def drain(self) -> None:
        """Wait until every submitted request has been emitted."""
        while self._pending > 0:
            if self._run_task is not None and self._run_task.done():
                log.warning("crawlee stopped with %d requests outstanding", self._pending)
                self._fail_outstanding("crawlee run loop ended")
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._idle.wait(), timeout=1.0)

    async def close(self) -> None:
        self._closed = True
        if self._crawler is not None:
            with contextlib.suppress(Exception):
                self._crawler.stop("polycrawl shutdown")
        if self._run_task is not None:
            task, self._run_task = self._run_task, None
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=20)
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await task
            except Exception as exc:  # pragma: no cover - teardown best effort
                log.debug("crawlee run task ended with %s", exc)
        # Only once the run loop is gone: dropping a queue out from under a live
        # worker would fail it mid-request. Storages are cached process-globally
        # by crawlee, so a long-lived process doing many crawls leaks every past
        # crawl's requests unless they are dropped here.
        await self._drop_storages()
        self._inflight.clear()

    async def _drop_storages(self) -> None:
        queue, self._request_queue = self._request_queue, None
        if queue is not None:
            with contextlib.suppress(Exception):
                await queue.drop()
        if self._crawler is not None:
            # Crawlee opens a key-value store of its own for statistics state.
            with contextlib.suppress(Exception):
                await (await self._crawler.get_key_value_store()).drop()

    # -- handlers ---------------------------------------------------------

    async def _on_page(self, context: Any) -> None:
        taken = self._take(context)
        if taken is None:
            return
        req, started = taken
        try:
            page = context.page
            b = self.config.browser
            if b.wait_for_selector:
                with contextlib.suppress(Exception):
                    await page.wait_for_selector(b.wait_for_selector, timeout=b.page_timeout_ms)
            if b.settle_ms:
                await asyncio.sleep(b.settle_ms / 1000.0)

            html = await page.content()
            response = context.response
            status_code = getattr(response, "status", None)
            headers = await _headers_of(response)
            final_url = getattr(response, "url", None) or page.url or req.url

            status = (
                FetchStatus.OK
                if status_code is None or status_code < 400
                else FetchStatus.HTTP_ERROR
            )
            result = FetchResult(
                request=req,
                status=status,
                url=final_url,
                http_status=status_code,
                html=html,
                headers=headers,
                elapsed=time.monotonic() - started,
                backend=self.name,
                error=None if status is FetchStatus.OK else f"HTTP {status_code}",
            )
        except Exception as exc:
            result = FetchResult(
                request=req,
                status=_classify(str(exc)),
                url=req.url,
                error=f"{type(exc).__name__}: {exc}",
                elapsed=time.monotonic() - started,
                backend=self.name,
            )
        await self._finish(result)

    async def _on_failure(self, context: Any, error: Exception) -> None:
        taken = self._take(context)
        if taken is None:
            return
        req, started = taken
        await self._finish(
            FetchResult(
                request=req,
                status=_classify(str(error)),
                url=req.url,
                error=f"{type(error).__name__}: {error}",
                elapsed=time.monotonic() - started,
                backend=self.name,
            )
        )

    def _take(self, context: Any) -> tuple[FetchRequest, float] | None:
        """Recover our request and its submit time from crawlee's ``user_data``."""
        try:
            user_data = context.request.user_data or {}
            key = user_data.get("polycrawl_key")
        except Exception:
            key = None
        if key is None:
            log.warning("crawlee returned a request without a polycrawl key; dropping")
            self._decrement()
            return None
        entry = self._inflight.pop(key, None)
        if entry is None:
            # Both the page handler and the failure handler fired for one
            # request; the second call has nothing left to emit.
            log.debug("duplicate completion for key %s", key)
            self._decrement()
            return None
        return entry

    async def _finish(self, result: FetchResult) -> None:
        try:
            await self.emit(result)
        finally:
            self._decrement()

    def _decrement(self) -> None:
        self._pending = max(0, self._pending - 1)
        if self._pending == 0:
            self._idle.set()

    def _fail_outstanding(self, reason: str) -> None:
        self._inflight.clear()
        self._pending = 0
        self._idle.set()
        log.warning("dropped outstanding crawlee requests: %s", reason)


def _blocking_args(browser: Any) -> list[str]:
    """Chromium flags implementing ``block_resources``.

    Done with launch flags rather than by intercepting requests: interception
    routes every request through Python, and aborting is not free on the page
    either -- a site that re-requests images we abort can spin, which is enough
    to stop it ever reaching ``load`` or ``networkidle``. A flag means the
    requests are never issued.

    ``media`` has no equivalent flag, so it is not blocked here; it is a small
    share of requests on a text crawl, and paying interception for it would cost
    more than it saves.
    """
    if not browser.block_resources or browser.browser_type != "chromium":
        return []
    blocked = set(browser.blocked_resource_types)
    args = []
    if "image" in blocked:
        args.append("--blink-settings=imagesEnabled=false")
    if "font" in blocked:
        args.append("--disable-remote-fonts")
    return args


_isolated_client_cls: type[Any] | None = None


def _isolated_storage_client() -> Any:
    """A storage client whose opened storages are private to one backend instance.

    Crawlee caches every storage it opens in a *process-global* instance manager,
    keyed by (storage type, name, storage-client key) -- and the default client
    key is just the client's class name. Two ``CrawleeBackend`` instances
    therefore resolve to the *same* cached request queue, whose ``asyncio.Lock``
    belongs to the event loop that first created it. A second crawl on a new
    loop -- a fresh ``asyncio.run``, a worker that rebuilds its loop, or
    pytest-asyncio's per-test loop -- then dies on ``Lock is bound to a
    different event loop``, and the crawl hangs with every request outstanding.

    Overriding the cache key per instance gives each crawl its own storages.
    Memory-backed, so nothing survives to the next crawl and no ``storage/``
    directory is written for what is throwaway bookkeeping -- the engine, not
    crawlee, owns the frontier and the output.
    """
    global _isolated_client_cls

    if _isolated_client_cls is None:
        from crawlee.storage_clients import MemoryStorageClient

        class IsolatedMemoryStorageClient(MemoryStorageClient):  # type: ignore[misc]
            def __init__(self) -> None:
                super().__init__()
                self._cache_key = f"polycrawl-{uuid4().hex}"

            def get_storage_client_cache_key(self, configuration: Any) -> str:
                return self._cache_key

        _isolated_client_cls = IsolatedMemoryStorageClient

    return _isolated_client_cls()


def _classify(message: str) -> FetchStatus:
    low = message.lower()
    if any(m in low for m in _TIMEOUT_MARKERS):
        return FetchStatus.TIMEOUT
    if any(m in low for m in _NETWORK_MARKERS):
        return FetchStatus.NETWORK_ERROR
    return FetchStatus.BACKEND_ERROR


async def _headers_of(response: Any) -> dict[str, str]:
    if response is None:
        return {}
    try:
        headers = response.headers
        if asyncio.iscoroutine(headers):
            headers = await headers
        if callable(headers):
            headers = headers()
            if asyncio.iscoroutine(headers):
                headers = await headers
        return {str(k).lower(): str(v) for k, v in dict(headers).items()}
    except Exception:
        return {}
