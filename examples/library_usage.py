"""Using polycrawl from your own code.

uv run python examples/library_usage.py
"""

from __future__ import annotations

import asyncio

from polycrawl import CrawlConfig, CrawlEngine, MemorySink


async def crawl_into_memory() -> None:
    """The common case: configure, run, read the pages back."""
    config = CrawlConfig(
        seeds=["https://quotes.toscrape.com/"],
        backend="scrapy",
        concurrency=8,
        max_pages=20,
        max_depth=2,
        scope="host",
        progress=False,
        # MemorySink is supplying the output, so no file is written.
        output={"format": "none"},
    )

    sink = MemorySink()
    metrics = await CrawlEngine(config, sink=sink).run()

    print(f"fetched {metrics.fetched} pages at {metrics.pages_per_second:.1f}/s")
    for page in sink.pages[:5]:
        print(f"  [{page.http_status}] {page.title[:60]!r} <- {page.url}")


async def crawl_without_a_browser() -> None:
    """The same crawl with no browser at all: faster, but no JavaScript.

    Worth checking whether a target actually needs rendering -- on a
    server-rendered site this path measured ~1.7x the throughput and less than
    half the per-page latency of the browser path, at a fraction of the memory.
    """
    config = CrawlConfig(
        seeds=["https://quotes.toscrape.com/"],
        backend="scrapy",
        backend_options={"render": False},
        concurrency=16,
        max_pages=20,
        scope="host",
        progress=False,
        output={"format": "none"},
    )
    sink = MemorySink()
    metrics = await CrawlEngine(config, sink=sink).run()
    print(f"no-browser: {metrics.fetched} pages at {metrics.pages_per_second:.1f}/s")


async def compare_backends() -> None:
    """Every backend should produce the same pages -- that is the design goal."""
    results = {}
    for backend in ("crawl4ai", "crawlee", "scrapy"):
        config = CrawlConfig(
            seeds=["https://quotes.toscrape.com/"],
            backend=backend,
            concurrency=8,
            max_pages=10,
            scope="host",
            progress=False,
            output={"format": "none"},
        )
        sink = MemorySink()
        await CrawlEngine(config, sink=sink).run()
        results[backend] = {p.url for p in sink.pages}

    reference, urls = next(iter(results.items()))
    for name, found in results.items():
        same = "same" if found == urls else "DIFFERENT"
        print(f"  {name}: {len(found)} urls ({same} as {reference})")


async def main() -> None:
    await crawl_into_memory()
    await crawl_without_a_browser()
    await compare_backends()


if __name__ == "__main__":
    asyncio.run(main())
