"""Writing your own fetch backend.

A backend is only a fetcher: it turns FetchRequests into FetchResults. The
frontier, dedup, robots, politeness, retries, extraction and output all stay in
the engine, so this file is the entire surface you have to implement.

    uv run python examples/custom_backend.py

To ship one from your own package instead of registering it at runtime, drop the
`@register` and advertise it in your pyproject.toml:

    [project.entry-points."polycrawl.backends"]
    httpx = "my_package.backend:HttpxBackend"

polycrawl then discovers it by name with no changes to this repository.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import ClassVar

from polycrawl import (
    BackendCapabilities,
    CrawlConfig,
    CrawledPage,
    CrawlEngine,
    CrawlerBackend,
    FetchRequest,
    FetchResult,
    FetchStatus,
    MemorySink,
    register,
)
from polycrawl.backend import EmitFn


@register
class HttpxBackend(CrawlerBackend):
    """A minimal HTTP backend built on httpx. No JavaScript, very fast."""

    name: ClassVar[str] = "httpx-example"
    install_extra: ClassVar[str] = "httpx"
    capabilities: ClassVar[BackendCapabilities] = BackendCapabilities(
        javascript=False,
        native_links=False,
        proxy=True,
    )

    @classmethod
    def is_available(cls) -> tuple[bool, str]:
        try:
            import httpx  # noqa: F401
        except ImportError as exc:
            return False, f"httpx is not installed ({exc})"
        return True, ""

    async def start(self, emit: EmitFn) -> None:
        import httpx

        self._emit = emit
        # Expensive, reusable setup belongs here rather than in submit().
        self._client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=self.config.browser.page_timeout_ms / 1000,
            limits=httpx.Limits(max_connections=self.config.concurrency),
        )
        self._tasks: set[asyncio.Task[None]] = set()

    async def submit(self, batch: Sequence[FetchRequest]) -> None:
        # Must not block until the fetches finish: hand the work off and return.
        for req in batch:
            task = asyncio.create_task(self._fetch(req))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _fetch(self, req: FetchRequest) -> None:
        started = time.monotonic()
        try:
            response = await self._client.get(req.url)
            status = FetchStatus.OK if response.status_code < 400 else FetchStatus.HTTP_ERROR
            result = FetchResult(
                request=req,
                status=status,
                url=str(response.url),
                http_status=response.status_code,
                html=response.text,
                headers=dict(response.headers),
                elapsed=time.monotonic() - started,
            )
        except Exception as exc:
            result = FetchResult(
                request=req,
                status=FetchStatus.NETWORK_ERROR,
                url=req.url,
                error=f"{type(exc).__name__}: {exc}",
                elapsed=time.monotonic() - started,
            )
        await self.emit(result)

    async def drain(self) -> None:
        # Filter to tasks that are still pending on every pass. Awaiting an
        # already-finished task does not yield to the loop, so the done callback
        # that empties this set would never get a chance to run.
        while pending := [t for t in self._tasks if not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    async def close(self) -> None:
        # Must be safe after a failed start, hence the getattr.
        client = getattr(self, "_client", None)
        if client is not None:
            await client.aclose()


async def main() -> None:
    available, reason = HttpxBackend.is_available()
    if not available:
        print(f"skipping: {reason}")
        return

    config = CrawlConfig(
        seeds=["https://quotes.toscrape.com/"],
        backend="httpx-example",  # resolved through the registry, by name
        concurrency=8,
        max_pages=10,
        max_depth=2,
        scope="host",
        progress=False,
        output={"format": "none"},
    )
    sink = MemorySink()
    metrics = await CrawlEngine(config, sink=sink).run()

    print(f"{metrics.fetched} pages at {metrics.pages_per_second:.1f}/s")
    page: CrawledPage
    for page in sink.pages[:5]:
        print(f"  [{page.http_status}] {page.title[:50]!r} {len(page.links)} links")


if __name__ == "__main__":
    asyncio.run(main())
