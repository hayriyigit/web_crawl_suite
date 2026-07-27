"""crawl4ai backend.

Wraps ``AsyncWebCrawler`` and its ``MemoryAdaptiveDispatcher``. crawl4ai is
strongest at *rendering*: it keeps a warm browser pool, reuses pages, and backs
off when system memory gets tight -- so this backend hands it whole batches and
lets its dispatcher schedule them, rather than driving one URL at a time.

The engine still owns the frontier and politeness, so crawl4ai's own caching
and deep-crawl features are deliberately switched off: two components deciding
what to fetch next would fight each other.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from ..backend import BackendCapabilities, CrawlerBackend, EmitFn
from ..models import FetchRequest, FetchResult, FetchStatus

if TYPE_CHECKING:
    from ..config import CrawlConfig

log = logging.getLogger("polycrawl.backends.crawl4ai")

__all__ = ["Crawl4AIBackend"]

# Error text -> status, so retry decisions are made on the right signal.
_TIMEOUT_MARKERS = ("timeout", "timed out", "navigation timeout")
_NETWORK_MARKERS = (
    "net::err",
    "connection",
    "dns",
    "ssl",
    "econnrefused",
    "unreachable",
    "socket",
)


class Crawl4AIBackend(CrawlerBackend):
    """Browser-rendered fetching via crawl4ai."""

    name: ClassVar[str] = "crawl4ai"
    install_extra: ClassVar[str] = "crawl4ai"
    capabilities: ClassVar[BackendCapabilities] = BackendCapabilities(
        javascript=True,
        native_links=True,
        native_markdown=True,
        proxy=True,
        stealth=True,
        screenshots=True,
    )

    def __init__(self, config: CrawlConfig) -> None:
        super().__init__(config)
        self._crawler: Any = None
        self._dispatcher: Any = None
        self._run_config: Any = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    @classmethod
    def is_available(cls) -> tuple[bool, str]:
        try:
            import crawl4ai  # noqa: F401
        except ImportError as exc:
            return False, f"crawl4ai is not installed ({exc})"
        return True, ""

    # -- lifecycle --------------------------------------------------------

    async def start(self, emit: EmitFn) -> None:
        from crawl4ai import (
            AsyncWebCrawler,
            BrowserConfig,
            CacheMode,
            CrawlerRunConfig,
            MemoryAdaptiveDispatcher,
        )

        self._emit = emit
        cfg = self.config
        b = cfg.browser

        browser_kwargs: dict[str, Any] = {
            "browser_type": b.browser_type,
            "headless": b.headless,
            "viewport_width": b.viewport_width,
            "viewport_height": b.viewport_height,
            "ignore_https_errors": b.ignore_https_errors,
            "java_script_enabled": True,
            # text_mode skips images/fonts at the browser level -- the single
            # biggest bandwidth saving for text crawling.
            "text_mode": b.block_resources,
            "light_mode": b.block_resources,
            "enable_stealth": b.stealth,
            "extra_args": list(b.extra_args),
            "verbose": cfg.verbose,
        }
        # crawl4ai derives client hints from the user agent and does not accept
        # None for it, so these are omitted rather than passed as None.
        if b.user_agent:
            browser_kwargs["user_agent"] = b.user_agent
        if cfg.proxy:
            browser_kwargs["proxy"] = cfg.proxy
        if b.max_pages_before_recycle:
            browser_kwargs["max_pages_before_recycle"] = b.max_pages_before_recycle

        browser_config = BrowserConfig(**browser_kwargs)

        self._run_config = CrawlerRunConfig(
            # domcontentloaded: DOM parsed and sync scripts run, without
            # waiting on images, fonts or never-idle analytics sockets.
            wait_until=b.wait_until,
            page_timeout=b.page_timeout_ms,
            wait_for=f"css:{b.wait_for_selector}" if b.wait_for_selector else None,
            delay_before_return_html=b.settle_ms / 1000.0 if b.settle_ms else 0.0,
            # The engine dedups; crawl4ai's cache would only add disk I/O.
            cache_mode=CacheMode.BYPASS,
            stream=True,
            exclude_all_images=b.block_resources,
            excluded_tags=["script", "style", "noscript"],
            scan_full_page=False,
            verbose=cfg.verbose,
            **dict(cfg.backend_options.get("run_config", {})),
        )

        self._dispatcher = MemoryAdaptiveDispatcher(
            memory_threshold_percent=cfg.memory_limit_pct,
            check_interval=0.5,
            # The engine already caps global in-flight work; this is the
            # backend-side ceiling for a single batch.
            max_session_permit=max(1, cfg.concurrency),
        )

        self._crawler = AsyncWebCrawler(config=browser_config)
        await self._crawler.start()
        log.debug("crawl4ai backend started (concurrency=%d)", cfg.concurrency)

    async def submit(self, batch: Sequence[FetchRequest]) -> None:
        if self._closed:
            raise RuntimeError("backend is closed")
        if not batch:
            return
        task = asyncio.create_task(self._run_batch(list(batch)))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        # Filter to genuinely pending tasks each pass. Looping on the set
        # itself would spin: awaiting already-finished tasks returns without
        # yielding, so the done-callbacks that remove them never get to run.
        while True:
            pending = [t for t in self._tasks if not t.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    async def close(self) -> None:
        self._closed = True
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        if self._crawler is not None:
            crawler, self._crawler = self._crawler, None
            try:
                await crawler.close()
            except Exception as exc:  # pragma: no cover - teardown best effort
                log.debug("crawl4ai close failed: %s", exc)

    # -- fetching ---------------------------------------------------------

    async def _run_batch(self, batch: list[FetchRequest]) -> None:
        """Fetch one batch, emitting each result the moment it lands."""
        by_url: dict[str, list[FetchRequest]] = {}
        for req in batch:
            by_url.setdefault(req.url, []).append(req)
        started = time.monotonic()

        try:
            stream = await self._crawler.arun_many(
                urls=[r.url for r in batch],
                config=self._run_config,
                dispatcher=self._dispatcher,
            )
            async for raw in stream:
                req = self._match(by_url, raw)
                if req is None:
                    continue
                await self.emit(self._to_result(req, raw, started))
        except asyncio.CancelledError:
            await self._emit_remaining(by_url, FetchStatus.BACKEND_ERROR, "cancelled", started)
            raise
        except Exception as exc:
            log.error("crawl4ai batch failed (%d urls): %s", len(batch), exc)
            await self._emit_remaining(
                by_url, FetchStatus.BACKEND_ERROR, f"{type(exc).__name__}: {exc}", started
            )
            return

        # Anything the dispatcher silently dropped must still be accounted for,
        # or the engine's in-flight counter never returns to zero.
        await self._emit_remaining(
            by_url, FetchStatus.BACKEND_ERROR, "no result returned by crawl4ai", started
        )

    @staticmethod
    def _match(by_url: dict[str, list[FetchRequest]], raw: Any) -> FetchRequest | None:
        """Pair a crawl4ai result back to its request, tolerating URL rewrites."""
        for candidate in (getattr(raw, "url", None), getattr(raw, "redirected_url", None)):
            if candidate and candidate in by_url:
                pending = by_url[candidate]
                req = pending.pop(0)
                if not pending:
                    del by_url[candidate]
                return req
        # crawl4ai normalises some URLs (e.g. appends a trailing slash); fall
        # back to the first outstanding request rather than losing the slot.
        if by_url:
            key = next(iter(by_url))
            pending = by_url[key]
            req = pending.pop(0)
            if not pending:
                del by_url[key]
            return req
        return None

    async def _emit_remaining(
        self,
        by_url: dict[str, list[FetchRequest]],
        status: FetchStatus,
        error: str,
        started: float,
    ) -> None:
        elapsed = time.monotonic() - started
        for pending in list(by_url.values()):
            for req in pending:
                await self.emit(
                    FetchResult(
                        request=req,
                        status=status,
                        url=req.url,
                        error=error,
                        elapsed=elapsed,
                        backend=self.name,
                    )
                )
        by_url.clear()

    def _to_result(self, req: FetchRequest, raw: Any, started: float) -> FetchResult:
        success = bool(getattr(raw, "success", False))
        status_code = getattr(raw, "status_code", None)
        error_message = getattr(raw, "error_message", None) or None
        final_url = getattr(raw, "redirected_url", None) or getattr(raw, "url", "") or req.url

        if success:
            status = FetchStatus.OK
        elif status_code and status_code >= 400:
            status = FetchStatus.HTTP_ERROR
        else:
            status = _classify(error_message)

        html = getattr(raw, "html", "") or ""
        elapsed = _elapsed_of(raw, started)

        return FetchResult(
            request=req,
            status=status,
            url=final_url,
            http_status=status_code,
            html=html,
            text=_markdown_of(raw),
            headers=_headers_of(raw),
            links=_links_of(raw) if success else None,
            error=error_message,
            elapsed=elapsed,
            backend=self.name,
        )


def _classify(error_message: str | None) -> FetchStatus:
    if not error_message:
        return FetchStatus.BACKEND_ERROR
    low = error_message.lower()
    if any(m in low for m in _TIMEOUT_MARKERS):
        return FetchStatus.TIMEOUT
    if any(m in low for m in _NETWORK_MARKERS):
        return FetchStatus.NETWORK_ERROR
    return FetchStatus.BACKEND_ERROR


def _headers_of(raw: Any) -> dict[str, str]:
    headers = getattr(raw, "response_headers", None)
    if not headers:
        return {}
    try:
        return {str(k).lower(): str(v) for k, v in dict(headers).items()}
    except Exception:
        return {}


def _markdown_of(raw: Any) -> str | None:
    md = getattr(raw, "markdown", None)
    if md is None:
        return None
    # crawl4ai returns either a str or a MarkdownGenerationResult.
    for attr in ("fit_markdown", "raw_markdown"):
        value = getattr(md, attr, None)
        if value:
            return str(value)
    return str(md) if isinstance(md, str) else None


def _links_of(raw: Any) -> list[str] | None:
    """Flatten crawl4ai's ``{internal: [...], external: [...]}`` link map."""
    links = getattr(raw, "links", None)
    if not isinstance(links, dict):
        return None
    out: list[str] = []
    for group in ("internal", "external"):
        for item in links.get(group) or ():
            href = item.get("href") if isinstance(item, dict) else getattr(item, "href", None)
            if href:
                out.append(href)
    return out or None


def _elapsed_of(raw: Any, started: float) -> float:
    dispatch = getattr(raw, "dispatch_result", None)
    for attr in ("duration", "elapsed"):
        value = getattr(dispatch, attr, None)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return time.monotonic() - started
