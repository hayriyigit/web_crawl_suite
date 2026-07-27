"""polycrawl -- a pluggable, high-throughput async web crawler.

The crawl pipeline (frontier, dedup, robots, politeness, retries, extraction,
output) is fixed; *how a URL is fetched* is pluggable. Backends implement a
four-method interface and are selected by name::

    import asyncio
    from polycrawl import CrawlConfig, crawl

    config = CrawlConfig(
        seeds=["https://example.com"],
        backend="crawl4ai",   # or "crawlee", "scrapy", or your own
        concurrency=32,
        max_pages=500,
    )
    metrics = asyncio.run(crawl(config))
    print(metrics.summary())

See :mod:`polycrawl.backend` for the backend contract and
:mod:`polycrawl.registry` for registering your own.
"""

from __future__ import annotations

from .backend import BackendCapabilities, CrawlerBackend
from .config import BrowserSettings, CrawlConfig, OutputSettings, PolitenessSettings
from .engine import CrawlEngine, crawl
from .metrics import Metrics
from .models import CrawledPage, FetchRequest, FetchResult, FetchStatus
from .registry import BackendInfo, get_backend, list_backends, register
from .service import FetchService, ServiceBusy, ServiceStats
from .sinks import JsonlSink, MemorySink, NullSink, Sink

__version__ = "0.3.0"

__all__ = [
    "BackendCapabilities",
    "BackendInfo",
    "BrowserSettings",
    "CrawlConfig",
    "CrawlEngine",
    "CrawledPage",
    "CrawlerBackend",
    "FetchRequest",
    "FetchResult",
    "FetchService",
    "FetchStatus",
    "JsonlSink",
    "MemorySink",
    "Metrics",
    "NullSink",
    "OutputSettings",
    "PolitenessSettings",
    "ServiceBusy",
    "ServiceStats",
    "Sink",
    "__version__",
    "crawl",
    "get_backend",
    "list_backends",
    "register",
]
