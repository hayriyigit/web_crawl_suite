"""Routing with a real browser, against the local test site.

The site serves both shapes the router has to tell apart: ``/page/N`` builds its
content in JavaScript, so a browserless fetch sees a placeholder, and
``/static/N`` is server-rendered and needs nothing. The assertions are that the
first escalates and the second does not -- and, just as importantly, that no
browser is launched at all for the second.

Thresholds are lowered from the defaults here because the fixture pages are tiny
(10 and 90 characters of text against 2500 in the default). The shapes are the
same; only the scale differs.
"""

from __future__ import annotations

import asyncio

import pytest
from server import LocalSite

from polycrawl import ContentCheck, CrawlConfig, RoutingFetchService
from polycrawl.registry import get_backend

pytestmark = pytest.mark.integration


def _available(name: str) -> bool:
    try:
        return get_backend(name).is_available()[0]
    except Exception:
        return False


@pytest.fixture
def site():  # type: ignore[no-untyped-def]
    with LocalSite() as s:
        yield s


def _router(site, **kwargs):  # type: ignore[no-untyped-def]
    cfg = CrawlConfig(
        seeds=[site.base_url],
        backend="scrapy",
        concurrency=4,
        progress=False,
        politeness={"respect_robots": False, "per_host_rps": 100, "per_host_concurrency": 4},
    )
    kwargs.setdefault("sufficient", ContentCheck(min_text=50, min_text_ratio=0.05))
    return RoutingFetchService.from_config(cfg, **kwargs)


@pytest.mark.skipif(not _available("scrapy"), reason="scrapy not installed")
class TestRoutingAgainstRealPages:
    async def test_javascript_page_escalates_and_gains_content(self, site) -> None:  # type: ignore[no-untyped-def]
        router = _router(site)
        async with router:
            page = await asyncio.wait_for(router.fetch_one(f"{site.base_url}/page/0"), timeout=120)

        # The marker exists only in the DOM after JS runs, so its presence is
        # proof the escalation happened and returned the rendered page.
        assert "marker-js-rendered-0" in page.text
        assert router.stats.escalated == 1
        assert router.stats.served_plain == 0

    async def test_server_rendered_page_never_launches_a_browser(self, site) -> None:  # type: ignore[no-untyped-def]
        router = _router(site)
        async with router:
            page = await asyncio.wait_for(
                router.fetch_one(f"{site.base_url}/static/0"), timeout=120
            )
            browser_started = router.rendered.ready

        assert "marker-server-rendered-0" in page.text
        assert router.stats.served_plain == 1
        assert router.stats.escalated == 0
        assert not browser_started, "a browser was launched for a page that did not need one"

    async def test_mixed_batch_reports_an_escalation_rate(self, site) -> None:  # type: ignore[no-untyped-def]
        router = _router(site)
        urls = [f"{site.base_url}/static/{i}" for i in range(3)]
        urls += [f"{site.base_url}/page/{i}" for i in range(3)]
        async with router:
            pages = await asyncio.wait_for(router.fetch(urls), timeout=180)

        assert [p.url for p in pages] == urls
        assert all(p.text for p in pages)
        snap = router.snapshot()["routing"]
        # Same host serves both shapes, so the per-host cache must not be what
        # decides this -- it is switched off for exactly that reason.
        assert snap["requested"] == 6
        assert snap["escalated"] == 3
        assert snap["escalation_rate"] == 0.5

    async def test_host_memory_skips_the_wasted_first_attempt(self, site) -> None:  # type: ignore[no-untyped-def]
        router = _router(site, host_ttl_s=3600)
        async with router:
            await asyncio.wait_for(router.fetch_one(f"{site.base_url}/page/0"), timeout=120)
            plain_calls_before = router.plain.stats.submitted
            await asyncio.wait_for(router.fetch_one(f"{site.base_url}/page/1"), timeout=120)
            plain_calls_after = router.plain.stats.submitted

        assert plain_calls_after == plain_calls_before, "the known-JS host was fetched twice"
        assert router.stats.host_cache_hits == 1


@pytest.mark.skipif(not _available("scrapy"), reason="scrapy not installed")
class TestMarkerPredicate:
    async def test_a_caller_supplied_marker_drives_the_decision(self, site) -> None:  # type: ignore[no-untyped-def]
        # The reliable signal when the caller knows what it is looking for: no
        # thresholds, no heuristic, just "is the thing I need present".
        router = _router(site, sufficient=ContentCheck(require_marker="marker-js-rendered"))
        async with router:
            page = await asyncio.wait_for(router.fetch_one(f"{site.base_url}/page/2"), timeout=120)

        assert "marker-js-rendered-2" in page.text
        assert router.snapshot()["routing"]["escalation_reasons"] == {"marker-missing": 1}
