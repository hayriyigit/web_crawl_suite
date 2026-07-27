"""Scrapy backend, optionally rendering with scrapy-playwright.

Two fetch modes from one backend:

``render=True`` (default)
    Pages go through scrapy-playwright, which drives the same Playwright
    Chromium the other backends use. That keeps the DOM-ready contract and makes
    scrapy's output comparable to crawl4ai's and crawlee's.
``render=False``
    Scrapy's own Twisted HTTP client, no browser at all. Several times faster
    and a fraction of the memory, but it sees only server-rendered HTML. Worth
    it whenever the target does not need JavaScript::

        backend_options: {render: false}

Why scrapy-playwright and not Splash: Splash renders with QtWebKit, an engine
frozen years before the frameworks it would have to run, and it needs a separate
service to be deployed alongside. Playwright renders with the same browser as
the other two backends, which is what lets all three produce identical pages.

Threading
---------
Scrapy runs on Twisted, and a Twisted reactor is installed once per *process*
and bound to one event loop for good. Doing that to the caller's loop would mean
a second crawl -- a new ``asyncio.run``, a worker rebuilding its loop, a test
suite with a loop per test -- inherits a reactor pinned to a dead loop. So the
reactor gets a private loop on its own thread (see :func:`_reactor_loop`) and
results are handed across to the engine's loop. Awaiting that hand-off inside
the scrapy callback is deliberate: it keeps the engine's bounded result queue
applying backpressure through the thread boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from ..backend import BackendCapabilities, CrawlerBackend, EmitFn
from ..models import FetchRequest, FetchResult, FetchStatus

if TYPE_CHECKING:
    from ..config import CrawlConfig

log = logging.getLogger("polycrawl.backends.scrapy")

__all__ = ["ScrapyBackend"]

_ASYNCIO_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"

_TIMEOUT_MARKERS = ("timeout", "timed out", "useragent", "took longer")
_NETWORK_MARKERS = (
    "dns",
    "connection",
    "connectionrefused",
    "connectionlost",
    "ssl",
    "net::err",
    "unreachable",
    "closed",
)

#: The reactor's loop and the thread driving it, created at most once per process.
_reactor_lock = threading.Lock()
_reactor_state: asyncio.AbstractEventLoop | None = None


def _reactor_loop() -> asyncio.AbstractEventLoop:
    """The event loop that owns Twisted's reactor, starting it if needed.

    A reactor cannot be replaced or rebound once installed, so it gets a loop
    that lives as long as the process rather than the caller's, which may be one
    of many. If the host application has already installed an asyncio reactor,
    that one is reused -- installing a second is not possible.
    """
    global _reactor_state

    with _reactor_lock:
        if _reactor_state is not None:
            return _reactor_state

        from scrapy.utils.reactor import install_reactor, is_reactor_installed

        if is_reactor_installed():
            from twisted.internet import reactor as installed

            loop = getattr(installed, "_asyncioEventloop", None)
            if loop is None:
                raise RuntimeError(
                    "a non-asyncio Twisted reactor is already installed in this process; "
                    "the scrapy backend needs "
                    f"{_ASYNCIO_REACTOR}. Import polycrawl before installing a reactor, "
                    "or select a different backend."
                )
            if not loop.is_running():
                raise RuntimeError(
                    "an asyncio Twisted reactor is installed but its event loop is not "
                    "running, so scrapy cannot be driven; start the reactor first."
                )
            _reactor_state = loop
            return loop

        ready = threading.Event()
        box: dict[str, Any] = {}

        def run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                # Reads the loop we just made current, and binds the reactor to it.
                install_reactor(_ASYNCIO_REACTOR)
                from twisted.internet import reactor
            except BaseException as exc:  # pragma: no cover - import/install failure
                box["error"] = exc
                ready.set()
                return
            box["loop"] = loop
            loop.call_soon(ready.set)
            # Drives loop.run_forever() and marks the reactor running, which parts
            # of Twisted check before scheduling.
            reactor.run(installSignalHandlers=False)

        threading.Thread(target=run, name="polycrawl-scrapy-reactor", daemon=True).start()
        if not ready.wait(60):  # pragma: no cover - pathological start-up
            raise RuntimeError("the scrapy reactor thread did not start within 60s")
        if "error" in box:
            raise RuntimeError(f"could not install the Twisted reactor: {box['error']}")

        _reactor_state = box["loop"]
        return _reactor_state


class ScrapyBackend(CrawlerBackend):
    """Fetching via Scrapy's downloader, with or without a browser."""

    name: ClassVar[str] = "scrapy"
    install_extra: ClassVar[str] = "scrapy"
    capabilities: ClassVar[BackendCapabilities] = BackendCapabilities(
        # With render=False this backend is HTML-only; see the module docstring.
        javascript=True,
        native_links=False,
        native_markdown=False,
        proxy=True,
        stealth=False,
        screenshots=False,
    )

    def __init__(self, config: CrawlConfig) -> None:
        super().__init__(config)
        self._crawler: Any = None
        self._run_task: asyncio.Task[Any] | None = None
        self._reactor: asyncio.AbstractEventLoop | None = None
        self._engine_loop: asyncio.AbstractEventLoop | None = None
        self._pending = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._closed = False
        self._render = bool(config.backend_options.get("render", True))

    @classmethod
    def is_available(cls) -> tuple[bool, str]:
        try:
            import scrapy
            from scrapy.crawler import Crawler
        except ImportError as exc:
            return False, f"scrapy is not installed ({exc})"
        if not hasattr(Crawler, "crawl_async"):
            import scrapy

            return False, (
                f"scrapy {scrapy.__version__} is too old; "
                "the asyncio entry point Crawler.crawl_async() needs scrapy >= 2.14"
            )
        try:
            import scrapy_playwright  # noqa: F401
        except ImportError as exc:
            return False, f"scrapy-playwright is not installed ({exc})"
        return True, ""

    # -- lifecycle --------------------------------------------------------

    async def start(self, emit: EmitFn) -> None:
        self._emit = emit
        self._engine_loop = asyncio.get_running_loop()
        self._reactor = _reactor_loop()

        settings = self._build_settings()
        self._crawler = await self._on_reactor(self._build_crawler(settings))
        log.debug(
            "scrapy backend started (concurrency=%d, render=%s)",
            self.config.concurrency,
            self._render,
        )
        if not self._render:
            log.info("scrapy backend is in HTML-only mode: JavaScript will not be executed")

    async def _build_crawler(self, settings: dict[str, Any]) -> Any:
        """Create and open a crawler. Runs on the reactor loop."""
        from scrapy.crawler import Crawler
        from scrapy.signals import spider_idle

        crawler = Crawler(_spider_class(), settings)
        # weak=False, and a module-level receiver rather than a closure: scrapy
        # connects signal receivers weakly, so a local handler is collected as
        # soon as this frame returns and the spider then closes on first idle.
        crawler.signals.connect(_stay_open, signal=spider_idle, weak=False)
        self._run_task = asyncio.ensure_future(crawler.crawl_async(backend=self))

        # engine.crawl() rejects requests until the spider is *open*, which is
        # later than crawl_async setting .engine and .spider.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self._run_task.done():
                # Surfaces a start-up failure (a missing browser, a bad setting)
                # instead of timing out on a crawler that never came up.
                self._run_task.result()
                raise RuntimeError("scrapy stopped before its spider was opened")
            if crawler.engine is not None and crawler.engine.spider is not None:
                return crawler
            await asyncio.sleep(0.02)
        raise RuntimeError("scrapy did not open its spider within 60s")

    def _build_settings(self) -> dict[str, Any]:
        cfg = self.config
        b = cfg.browser

        settings: dict[str, Any] = {
            "TWISTED_REACTOR": _ASYNCIO_REACTOR,
            # The engine is the single authority on all of the below; a second
            # policy inside scrapy would silently fight it.
            "ROBOTSTXT_OBEY": False,
            "RETRY_ENABLED": False,
            "AUTOTHROTTLE_ENABLED": False,
            "DOWNLOAD_DELAY": 0,
            "RANDOMIZE_DOWNLOAD_DELAY": False,
            "DEPTH_LIMIT": 0,
            "CONCURRENT_REQUESTS": max(1, cfg.concurrency),
            # Per-host pacing is the engine's HostLimiter, which is adaptive;
            # scrapy's fixed cap would only ever be the lower of the two.
            "CONCURRENT_REQUESTS_PER_DOMAIN": max(1, cfg.concurrency),
            "DOWNLOAD_TIMEOUT": b.page_timeout_ms / 1000.0,
            # Non-2xx must reach our callback rather than being dropped as an
            # error, so the engine can record and report the status itself.
            "HTTPERROR_ALLOW_ALL": True,
            "LOG_ENABLED": cfg.verbose,
            "LOG_LEVEL": "INFO" if cfg.verbose else "ERROR",
            "LOGSTATS_INTERVAL": 0,
            "TELNETCONSOLE_ENABLED": False,
            # Nothing here produces scrapy items; the engine writes the output.
            "ITEM_PIPELINES": {},
        }
        if b.user_agent:
            settings["USER_AGENT"] = b.user_agent

        if self._render:
            settings.update(self._playwright_settings())

        settings.update(dict(cfg.backend_options.get("settings", {})))
        return settings

    def _playwright_settings(self) -> dict[str, Any]:
        cfg = self.config
        b = cfg.browser

        args = list(b.extra_args)
        blocked = set(b.blocked_resource_types) if b.block_resources else set()
        # Block at the browser level wherever a flag can express it, because the
        # alternative -- PLAYWRIGHT_ABORT_REQUEST -- routes *every* request
        # through Python, and aborting is not free on the page either: a real
        # live-scores site re-requested the images we aborted in a loop, 16,550
        # requests in 25s, and never reached its `load` event. A flag means the
        # requests are never issued at all.
        if b.browser_type == "chromium":
            if "image" in blocked:
                args.append("--blink-settings=imagesEnabled=false")
                blocked.discard("image")
            if "font" in blocked:
                args.append("--disable-remote-fonts")
                blocked.discard("font")

        launch: dict[str, Any] = {"headless": b.headless}
        if args:
            launch["args"] = args
        if cfg.proxy:
            launch["proxy"] = {"server": cfg.proxy}

        context: dict[str, Any] = {
            "viewport": {"width": b.viewport_width, "height": b.viewport_height},
            "ignore_https_errors": b.ignore_https_errors,
        }
        if b.user_agent:
            context["user_agent"] = b.user_agent

        handler = _shared_playwright_handler()
        settings: dict[str, Any] = {
            # Both schemes: a crawl can follow a link from https to http, and a
            # scheme handled without the browser would silently skip JavaScript
            # for those pages.
            "DOWNLOAD_HANDLERS": {"http": handler, "https": handler},
            "PLAYWRIGHT_BROWSER_TYPE": b.browser_type,
            "PLAYWRIGHT_LAUNCH_OPTIONS": launch,
            "PLAYWRIGHT_CONTEXTS": {"default": context},
            "PLAYWRIGHT_MAX_PAGES_PER_CONTEXT": max(1, cfg.concurrency),
            "PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT": b.page_timeout_ms,
        }
        if blocked:
            # Whatever the flags could not express. The predicate is consulted for
            # every request whatever its type, so it is wired up only when
            # something is genuinely left to block.
            remaining = frozenset(blocked)

            def abort(request: Any) -> bool:
                return request.resource_type in remaining

            settings["PLAYWRIGHT_ABORT_REQUEST"] = abort
        return settings

    # -- submission -------------------------------------------------------

    async def submit(self, batch: Sequence[FetchRequest]) -> None:
        if self._closed:
            raise RuntimeError("backend is closed")
        if not batch:
            return
        if self._run_task is not None and self._run_task.done():
            raise RuntimeError("scrapy crawl is not active")

        requests = [self._to_scrapy(req) for req in batch]
        self._pending += len(requests)
        self._idle.clear()
        try:
            await self._on_reactor(self._inject(requests))
        except Exception:
            # Nothing was queued, so nothing will ever be emitted for them.
            for _ in requests:
                self._decrement()
            raise

    def _to_scrapy(self, req: FetchRequest) -> Any:
        import scrapy

        cfg = self.config
        b = cfg.browser
        spider = self._crawler.spider

        # The FetchRequest rides along in meta: scrapy keeps meta identity across
        # redirects and never serialises it here, so unlike crawlee there is no
        # need for a side table keyed by an id.
        meta: dict[str, Any] = {
            "polycrawl_request": req,
            "polycrawl_submitted_at": time.monotonic(),
        }
        if cfg.proxy and not self._render:
            meta["proxy"] = cfg.proxy
        if self._render:
            meta["playwright"] = True
            meta["playwright_page_goto_kwargs"] = {"wait_until": b.wait_until}
            methods = []
            if b.wait_for_selector:
                from scrapy_playwright.page import PageMethod

                methods.append(
                    PageMethod("wait_for_selector", b.wait_for_selector, timeout=b.page_timeout_ms)
                )
            if b.settle_ms:
                from scrapy_playwright.page import PageMethod

                methods.append(PageMethod("wait_for_timeout", b.settle_ms))
            if methods:
                meta["playwright_page_methods"] = methods

        return scrapy.Request(
            url=req.url,
            callback=spider.handle_response,
            errback=spider.handle_error,
            meta=meta,
            # The engine already deduped; scrapy's filter would drop legitimate
            # re-fetches such as a retry of the same URL.
            dont_filter=True,
        )

    async def _inject(self, requests: Sequence[Any]) -> None:
        """Hand requests to scrapy's engine. Runs on the reactor loop."""
        engine = self._crawler.engine
        for request in requests:
            engine.crawl(request)

    async def drain(self) -> None:
        while self._pending > 0:
            if self._run_task is not None and self._run_task.done():
                log.warning("scrapy stopped with %d requests outstanding", self._pending)
                self._fail_outstanding("scrapy crawl ended")
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._idle.wait(), timeout=1.0)

    async def close(self) -> None:
        self._closed = True
        crawler, self._crawler = self._crawler, None
        if crawler is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._on_reactor(crawler.stop_async()), timeout=30)
        task, self._run_task = self._run_task, None
        if task is not None and not task.done():
            self._reactor_call(task.cancel)
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await self._await_reactor_task(task)

    # -- results ----------------------------------------------------------

    async def on_response(self, response: Any) -> None:
        """Scrapy callback. Runs on the reactor loop."""
        req, started = _taken(response.meta)
        if req is None:
            log.warning("scrapy returned a response without a polycrawl request; dropping")
            return
        status = getattr(response, "status", None)
        result = FetchResult(
            request=req,
            status=FetchStatus.OK if status is None or status < 400 else FetchStatus.HTTP_ERROR,
            url=response.url or req.url,
            http_status=status,
            html=_text_of(response),
            headers=_headers_of(response),
            elapsed=time.monotonic() - started,
            backend=self.name,
            error=None if status is None or status < 400 else f"HTTP {status}",
        )
        await self._hand_over(result)

    async def on_error(self, failure: Any) -> None:
        """Scrapy errback. Runs on the reactor loop."""
        request = getattr(failure, "request", None) or getattr(failure.value, "request", None)
        meta = getattr(request, "meta", None) or {}
        req, started = _taken(meta)
        if req is None:
            log.warning("scrapy failed a request without a polycrawl request; dropping")
            return
        exc = failure.value
        await self._hand_over(
            FetchResult(
                request=req,
                status=_classify(f"{type(exc).__name__}: {exc}"),
                url=req.url,
                error=f"{type(exc).__name__}: {exc}",
                elapsed=time.monotonic() - started,
                backend=self.name,
            )
        )

    async def _hand_over(self, result: FetchResult) -> None:
        """Move one result from the reactor loop to the engine's loop.

        Awaited rather than fired and forgotten: ``emit`` blocks while the
        engine's result queue is full, and that is the backpressure signal --
        dropping it here would let scrapy fetch without limit while parsing fell
        behind.
        """
        loop = self._engine_loop
        if loop is None or loop.is_closed():  # pragma: no cover - post-teardown
            log.debug("engine loop is gone; discarding result for %s", result.url)
            return
        future = asyncio.run_coroutine_threadsafe(self._finish(result), loop)
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wrap_future(future)

    async def _finish(self, result: FetchResult) -> None:
        """Runs on the engine's loop, which owns ``_pending`` and ``_idle``."""
        try:
            await self.emit(result)
        finally:
            self._decrement()

    def _decrement(self) -> None:
        self._pending = max(0, self._pending - 1)
        if self._pending == 0:
            self._idle.set()

    def _fail_outstanding(self, reason: str) -> None:
        self._pending = 0
        self._idle.set()
        log.warning("dropped outstanding scrapy requests: %s", reason)

    # -- cross-loop plumbing ----------------------------------------------

    async def _on_reactor(self, coro: Any) -> Any:
        """Run a coroutine on the reactor loop and await it from ours."""
        reactor = self._reactor
        if reactor is None:  # pragma: no cover - start() sets this
            raise RuntimeError("scrapy backend was not started")
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, reactor))

    def _reactor_call(self, fn: Any) -> None:
        if self._reactor is not None and not self._reactor.is_closed():
            self._reactor.call_soon_threadsafe(fn)

    async def _await_reactor_task(self, task: asyncio.Task[Any]) -> None:
        """Wait for a task that belongs to the reactor loop, from ours."""
        for _ in range(600):
            if task.done():
                return
            await asyncio.sleep(0.05)


_handler_cls: type[Any] | None = None


def _shared_playwright_handler() -> type[Any]:
    """A playwright download handler that both URL schemes share.

    Scrapy builds one download handler per registered scheme, eagerly for
    handlers that set ``lazy = False`` as this one does -- and every
    scrapy-playwright handler launches a browser of its own. The configuration
    its README documents (http *and* https) therefore costs two browsers per
    crawl, roughly doubling the memory the other backends need for the same
    work. Both schemes have to be registered, so instead they resolve to one
    instance and one browser.
    """
    global _handler_cls

    if _handler_cls is None:
        from scrapy_playwright.handler import ScrapyPlaywrightDownloadHandler

        class SharedPlaywrightHandler(ScrapyPlaywrightDownloadHandler):  # type: ignore[misc]
            #: Set once the browser has been torn down. Scrapy closes each
            #: scheme's handler, which is now the same object twice.
            _polycrawl_closed = False

            @classmethod
            def from_crawler(cls, crawler: Any) -> Any:
                existing = getattr(crawler, "_polycrawl_pw_handler", None)
                if existing is not None:
                    return existing
                handler = super().from_crawler(crawler)
                # Held on the crawler so it cannot outlive the crawl.
                crawler._polycrawl_pw_handler = handler
                return handler

            async def close(self) -> None:
                if self._polycrawl_closed:
                    return
                self._polycrawl_closed = True
                await super().close()

        _handler_cls = SharedPlaywrightHandler

    return _handler_cls


def _stay_open(*_: object, **__: object) -> None:
    """Keep a spider alive when its queue drains.

    Scrapy closes a spider the moment it runs out of requests. Here the engine
    supplies work in batches, so an empty queue is the normal state between
    batches rather than the end of the crawl.
    """
    from scrapy.exceptions import DontCloseSpider

    raise DontCloseSpider


_spider_cls: type[Any] | None = None


def _spider_class() -> type[Any]:
    """The spider carrying our callbacks, built on first use.

    Defined lazily because scrapy is an optional dependency: importing this
    module must not require it, so there is no ``scrapy.Spider`` to subclass at
    import time.
    """
    global _spider_cls

    if _spider_cls is None:
        import scrapy

        class PolycrawlSpider(scrapy.Spider):  # type: ignore[misc]
            """Produces no work of its own; the engine supplies every request."""

            name = "polycrawl"

            async def start(self) -> Any:  # type: ignore[override]
                # No start URLs: requests arrive through ScrapyBackend.submit().
                # Scrapy expects an async generator, hence the unreachable yield.
                return
                yield  # pragma: no cover

            async def handle_response(self, response: Any) -> None:
                await self.backend.on_response(response)

            async def handle_error(self, failure: Any) -> None:
                await self.backend.on_error(failure)

        _spider_cls = PolycrawlSpider

    return _spider_cls


def _taken(meta: dict[str, Any]) -> tuple[FetchRequest | None, float]:
    req = meta.get("polycrawl_request")
    started = meta.get("polycrawl_submitted_at") or time.monotonic()
    return (req if isinstance(req, FetchRequest) else None), float(started)


def _text_of(response: Any) -> str:
    """Decoded body, tolerating responses scrapy could not decode as text."""
    try:
        return response.text
    except (AttributeError, UnicodeDecodeError):
        body = getattr(response, "body", b"") or b""
        return body.decode("utf-8", "replace")


def _headers_of(response: Any) -> dict[str, str]:
    headers = getattr(response, "headers", None)
    if not headers:
        return {}
    out: dict[str, str] = {}
    try:
        for key, value in headers.items():
            name = key.decode("latin-1") if isinstance(key, bytes) else str(key)
            if isinstance(value, (list, tuple)):
                value = value[0] if value else b""
            text = value.decode("latin-1") if isinstance(value, bytes) else str(value)
            out[name.lower()] = text
    except Exception:  # pragma: no cover - defensive
        return {}
    return out


def _classify(message: str) -> FetchStatus:
    low = message.lower()
    if any(m in low for m in _TIMEOUT_MARKERS):
        return FetchStatus.TIMEOUT
    if any(m in low for m in _NETWORK_MARKERS):
        return FetchStatus.NETWORK_ERROR
    return FetchStatus.BACKEND_ERROR
