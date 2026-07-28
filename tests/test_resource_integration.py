"""What the browser actually asks the server for, with a real browser.

These assert from the *server's* side. Whether a request was made at all does
not appear anywhere in the crawl output, so nothing here can be checked by
inspecting pages -- which is precisely why a prefetch storm and a dead HTTP
cache both went unnoticed until a fixture started counting.

Only the browser backends are covered; without rendering there is no speculative
navigation and no subresource to cache.
"""

from __future__ import annotations

import asyncio

import pytest
from server import LocalSite

from polycrawl import CrawlConfig, CrawlEngine, MemorySink
from polycrawl.registry import get_backend

pytestmark = pytest.mark.integration

RENDERING_BACKENDS = ["crawlee", "scrapy"]


def _available(name: str) -> bool:
    try:
        return get_backend(name).is_available()[0]
    except Exception:
        return False


@pytest.fixture
def site():  # type: ignore[no-untyped-def]
    with LocalSite() as s:
        s.reset_hits()
        yield s


def _config(site, backend: str, **browser):  # type: ignore[no-untyped-def]
    return CrawlConfig(
        seeds=[f"{site.base_url}/res/0"],
        backend=backend,
        concurrency=4,
        batch_size=4,
        max_pages=4,
        max_depth=3,
        scope="host",
        progress=False,
        output={"format": "none"},
        politeness={
            "respect_robots": False,
            "per_host_rps": 100,
            "per_host_concurrency": 4,
            "adaptive": False,
        },
        browser=browser,
    )


async def _crawl(cfg: CrawlConfig, timeout: float = 180) -> MemorySink:
    sink = MemorySink()
    engine = CrawlEngine(cfg, sink=sink)
    await asyncio.wait_for(engine.run(), timeout=timeout)
    return sink


async def _crawl_with_backend(cfg: CrawlConfig, timeout: float = 180):  # type: ignore[no-untyped-def]
    """Crawl with a backend we keep a handle on, so its trace can be read.

    The engine only closes a backend it created itself, and crawlee runs with
    ``keep_alive=True``, so an injected one left open never stops.
    """
    backend = get_backend(cfg.backend)(cfg)
    sink = MemorySink()
    try:
        await asyncio.wait_for(CrawlEngine(cfg, sink=sink, backend=backend).run(), timeout=timeout)
    finally:
        await backend.close()
    return backend, sink


@pytest.mark.parametrize("backend", RENDERING_BACKENDS)
class TestPrefetchBlocking:
    async def test_prefetch_reaches_the_server_by_default(self, site, backend: str) -> None:  # type: ignore[no-untyped-def]
        if not _available(backend):
            pytest.skip(f"{backend} not installed")
        await _crawl(_config(site, backend))
        hits = site.hits()
        # Establishes the baseline the blocking test is measured against: if the
        # fixture ever stops advertising prefetch links, that test would pass
        # for the wrong reason.
        assert hits.get("res-prefetch", 0) > 0

    async def test_block_prefetch_stops_them(self, site, backend: str) -> None:  # type: ignore[no-untyped-def]
        if not _available(backend):
            pytest.skip(f"{backend} not installed")
        sink = await _crawl(_config(site, backend, block_prefetch=True))
        hits = site.hits()
        assert hits.get("res-prefetch", 0) == 0
        # The pages themselves must still arrive -- blocking speculative
        # navigation must not touch real ones.
        assert hits.get("res-page", 0) > 0
        assert len(sink.pages) >= 2
        assert all("marker-resource" in p.text for p in sink.pages)


@pytest.mark.parametrize("backend", RENDERING_BACKENDS)
class TestHostDenylist:
    async def test_beacon_reaches_the_server_by_default(self, site, backend: str) -> None:  # type: ignore[no-untyped-def]
        if not _available(backend):
            pytest.skip(f"{backend} not installed")
        await _crawl(_config(site, backend))
        assert site.hits().get("beacon", 0) > 0

    async def test_blocked_host_is_not_contacted(self, site, backend: str) -> None:  # type: ignore[no-untyped-def]
        if not _available(backend):
            pytest.skip(f"{backend} not installed")
        # The beacon goes to `localhost` while the crawl runs against
        # `127.0.0.1`, so this blocks a third party without blocking the site.
        sink = await _crawl(_config(site, backend, blocked_hosts=["localhost"]))
        assert site.hits().get("beacon", 0) == 0
        assert len(sink.pages) >= 2


class TestResourceTrace:
    async def test_trace_records_what_was_fetched(self, site) -> None:  # type: ignore[no-untyped-def]
        if not _available("crawlee"):
            pytest.skip("crawlee not installed")
        cfg = _config(site, "crawlee", trace_resources=True)
        backend, sink = await _crawl_with_backend(cfg)

        trace = backend.resource_trace
        assert trace is not None
        assert trace.pages == len(sink.pages)
        assert trace.requests["document"] >= len(sink.pages)
        assert trace.requests["stylesheet"] >= 1
        assert trace.total_bytes > 0

    async def test_trace_sees_the_cache_being_used(self, site) -> None:  # type: ignore[no-untyped-def]
        if not _available("crawlee"):
            pytest.skip("crawlee not installed")
        # One stylesheet shared by every page, and no route installed, so the
        # browser should serve it from cache after the first page. This is the
        # regression guard for the 1.63x cache loss: if a route ever gets
        # installed unconditionally, the hit count goes to zero.
        cfg = _config(site, "crawlee", trace_resources=True)
        backend, _ = await _crawl_with_backend(cfg)

        trace = backend.resource_trace
        assert trace is not None
        assert trace.from_cache > 0, "shared assets were refetched instead of served from cache"
        assert site.hits().get("css", 0) < trace.requests["stylesheet"]

    async def test_blocking_costs_the_cache(self, site) -> None:  # type: ignore[no-untyped-def]
        if not _available("crawlee"):
            pytest.skip("crawlee not installed")
        # The documented trade-off, asserted rather than only written down: with
        # a route installed the same shared stylesheet is fetched per page.
        cfg = _config(site, "crawlee", trace_resources=True, block_prefetch=True)
        _, sink = await _crawl_with_backend(cfg)

        assert site.hits().get("css", 0) >= len(sink.pages)
