"""Routing between a browserless tier and a browser tier.

No browser is launched here: both tiers are stubs, so the tests are about the
decision logic -- when to escalate, what to keep, and what must not be paid for
twice.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from polycrawl.models import CrawledPage
from polycrawl.routing import ContentCheck, RoutingFetchService, Verdict


def page(
    url: str = "https://example.test/a",
    *,
    text: str = "x" * 5000,
    html: str | None = None,
    status: str = "ok",
    http_status: int | None = 200,
    backend: str = "stub",
) -> CrawledPage:
    return CrawledPage(
        url=url,
        final_url=url,
        depth=0,
        parent=None,
        http_status=http_status,
        status=status,
        title="t",
        text=text,
        html=html if html is not None else f"<html><body>{text}</body></html>",
        links=[],
        n_links=0,
        error=None,
        elapsed=0.01,
        backend=backend,
        fetched_at=0.0,
    )


class StubService:
    """Enough of FetchService for the router to drive."""

    def __init__(self, result: Any = None, *, fail_start: bool = False) -> None:
        self.result = result if result is not None else page()
        self.fail_start = fail_start
        self.ready = False
        self.started = 0
        self.closed = 0
        self.calls: list[str] = []
        self.limiter = object()
        self.robots = object()

    async def start(self) -> None:
        self.started += 1
        if self.fail_start:
            raise RuntimeError("no browser here")
        self.ready = True

    async def close(self) -> None:
        self.closed += 1
        self.ready = False

    async def fetch_one(self, url: str) -> CrawledPage:
        self.calls.append(url)
        if callable(self.result):
            return self.result(url)
        return self.result

    def snapshot(self) -> dict[str, Any]:
        return {"calls": len(self.calls)}


def build(plain: StubService, rendered: StubService, **kwargs: Any) -> RoutingFetchService:
    return RoutingFetchService(plain, rendered, **kwargs)  # type: ignore[arg-type]


class TestContentCheck:
    def test_long_prose_is_sufficient(self) -> None:
        assert ContentCheck()(page(text="y" * 6000)).sufficient

    def test_thin_text_escalates(self) -> None:
        verdict = ContentCheck()(page(text="short"))
        assert not verdict.sufficient
        assert "thin-text" in verdict.reason

    def test_low_text_ratio_escalates(self) -> None:
        # Plenty of text, but buried in markup: the un-hydrated SPA shape, and
        # the case a text-length floor alone would wave through.
        text = "z" * 4000
        verdict = ContentCheck()(
            page(text=text, html="<div>" + "<span></span>" * 40_000 + "</div>")
        )
        assert not verdict.sufficient
        assert "low-text-ratio" in verdict.reason

    def test_marker_present_in_text(self) -> None:
        check = ContentCheck(require_marker="Exchange rates")
        assert check(page(text="tiny but has Exchange rates in it")).sufficient

    def test_marker_missing_escalates_even_with_lots_of_text(self) -> None:
        check = ContentCheck(require_marker="Exchange rates")
        assert not check(page(text="q" * 9000)).sufficient

    def test_marker_in_html_only_does_not_count(self) -> None:
        # The marker for JS-built content is usually already in the raw HTML,
        # sitting inside the script that has not run yet. Matching that would
        # declare success on precisely the page that needs a browser.
        check = ContentCheck(require_marker="Exchange rates")
        html = "<script>var t = '<p>Exchange rates</p>';</script>"
        assert not check(page(text="Loading...", html=html)).sufficient

    def test_403_escalates(self) -> None:
        # Often anti-bot, which a real browser frequently passes.
        verdict = ContentCheck()(page(status="http_error", http_status=403, text=""))
        assert not verdict.sufficient
        assert verdict.reason == "http-403"

    def test_404_is_not_escalated(self) -> None:
        # A 404 is a 404; rendering it again only burns a browser slot.
        assert ContentCheck()(page(status="http_error", http_status=404, text="")).sufficient


class TestRouting:
    async def test_sufficient_page_never_starts_the_browser(self) -> None:
        plain, rendered = StubService(page(text="w" * 6000)), StubService()
        router = build(plain, rendered)
        await router.start()
        result = await router.fetch_one("https://example.test/a")

        assert result.backend == "stub"
        assert rendered.started == 0, "a browser was launched for a page that did not need one"
        assert router.stats.served_plain == 1
        assert router.stats.escalated == 0

    async def test_thin_page_escalates(self) -> None:
        plain = StubService(page(text="tiny"))
        rendered = StubService(page(text="R" * 6000, backend="browser"))
        router = build(plain, rendered)
        await router.start()
        result = await router.fetch_one("https://example.test/a")

        assert result.backend == "browser"
        assert rendered.started == 1
        assert router.stats.escalated == 1

    async def test_browser_starts_once_across_many_escalations(self) -> None:
        plain = StubService(page(text="tiny"))
        rendered = StubService(page(text="R" * 6000, backend="browser"))
        router = build(plain, rendered, host_ttl_s=0)
        await router.start()
        await router.fetch([f"https://example.test/{i}" for i in range(8)])

        assert rendered.started == 1

    async def test_failed_escalation_falls_back_to_the_plain_page(self) -> None:
        # The browser tier is the optional half. Losing it must degrade to the
        # browserless answer, not to an error.
        plain = StubService(page(text="tiny"))
        rendered = StubService(fail_start=True)
        router = build(plain, rendered)
        await router.start()
        result = await router.fetch_one("https://example.test/a")

        assert result.text == "tiny"
        assert router.stats.escalation_failed == 1

    async def test_rendered_result_is_discarded_when_it_has_less(self) -> None:
        # A browser can fail in ways that still produce a page -- a consent wall,
        # a timeout mid-load -- and returning less than the free attempt already
        # got would be a regression.
        plain = StubService(page(text="a" * 2000))
        rendered = StubService(page(text="short", backend="browser"))
        router = build(plain, rendered)
        await router.start()
        result = await router.fetch_one("https://example.test/a")

        assert result.backend == "stub"
        assert len(result.text) == 2000
        assert router.stats.rendered_not_better == 1

    async def test_results_keep_the_requested_order(self) -> None:
        def by_url(url: str) -> CrawledPage:
            return page(url, text="w" * 6000)

        router = build(StubService(by_url), StubService())
        await router.start()
        urls = [f"https://example.test/{i}" for i in range(6)]
        assert [p.url for p in await router.fetch(urls)] == urls

    async def test_empty_batch(self) -> None:
        router = build(StubService(), StubService())
        await router.start()
        assert await router.fetch([]) == []

    async def test_fetch_before_start_is_an_error(self) -> None:
        router = build(StubService(), StubService())
        with pytest.raises(RuntimeError):
            await router.fetch_one("https://example.test/a")


class TestHostMemory:
    async def test_second_url_from_a_rendering_host_skips_the_plain_attempt(self) -> None:
        plain = StubService(page(text="tiny"))
        rendered = StubService(page(text="R" * 6000, backend="browser"))
        router = build(plain, rendered)
        await router.start()

        await router.fetch_one("https://example.test/a")
        assert len(plain.calls) == 1
        await router.fetch_one("https://example.test/b")

        # Still one: the host is known to need a browser, so the free attempt
        # that would certainly come back thin is skipped.
        assert len(plain.calls) == 1
        assert router.stats.host_cache_hits == 1

    async def test_a_different_host_is_judged_on_its_own(self) -> None:
        plain = StubService(page(text="tiny"))
        rendered = StubService(page(text="R" * 6000, backend="browser"))
        router = build(plain, rendered)
        await router.start()

        await router.fetch_one("https://a.test/x")
        await router.fetch_one("https://b.test/x")
        assert len(plain.calls) == 2

    async def test_the_decision_expires(self) -> None:
        plain = StubService(page(text="tiny"))
        rendered = StubService(page(text="R" * 6000, backend="browser"))
        router = build(plain, rendered, host_ttl_s=3600)
        await router.start()
        await router.fetch_one("https://example.test/a")

        # Sites get rebuilt; a verdict from an hour ago should not be permanent.
        host = "example.test"
        needs, _ = router._host_verdicts[host]
        router._host_verdicts[host] = (needs, time.monotonic() - 1)

        await router.fetch_one("https://example.test/b")
        assert len(plain.calls) == 2

    async def test_ttl_zero_disables_the_cache(self) -> None:
        plain = StubService(page(text="tiny"))
        rendered = StubService(page(text="R" * 6000, backend="browser"))
        router = build(plain, rendered, host_ttl_s=0)
        await router.start()
        await router.fetch_one("https://example.test/a")
        await router.fetch_one("https://example.test/b")
        assert len(plain.calls) == 2


class TestSharedState:
    def test_both_tiers_share_one_limiter_and_robots_cache(self) -> None:
        # Two limiters would pace a host twice over and fetch its robots.txt
        # twice -- the defect docs/deployment.md warns about across processes,
        # reintroduced inside one.
        plain, rendered = StubService(), StubService()
        build(plain, rendered)
        assert rendered.limiter is plain.limiter
        assert rendered.robots is plain.robots


class TestCustomPredicate:
    async def test_a_plain_bool_predicate_is_accepted(self) -> None:
        plain = StubService(page(text="tiny"))
        rendered = StubService(page(text="R" * 100, backend="browser"))
        router = build(plain, rendered, sufficient=lambda _page: True)
        await router.start()
        result = await router.fetch_one("https://example.test/a")
        assert result.backend == "stub"

    async def test_the_reason_reaches_the_stats(self) -> None:
        plain = StubService(page(text="tiny"))
        rendered = StubService(page(text="R" * 6000, backend="browser"))
        router = build(plain, rendered, sufficient=lambda _p: Verdict(False, "needs-price-table"))
        await router.start()
        await router.fetch_one("https://example.test/a")
        assert router.snapshot()["routing"]["escalation_reasons"] == {"needs-price-table": 1}


class TestSnapshot:
    async def test_escalation_rate(self) -> None:
        calls = {"n": 0}

        def alternating(url: str) -> CrawledPage:
            calls["n"] += 1
            return page(url, text="tiny" if calls["n"] % 2 else "w" * 6000)

        plain = StubService(alternating)
        rendered = StubService(page(text="R" * 6000, backend="browser"))
        router = build(plain, rendered, host_ttl_s=0)
        await router.start()
        for i in range(4):
            await router.fetch_one(f"https://example.test/{i}")

        snap = router.snapshot()["routing"]
        assert snap["requested"] == 4
        assert snap["escalated"] == 2
        assert snap["escalation_rate"] == 0.5

    async def test_browser_started_is_reported(self) -> None:
        plain = StubService(page(text="w" * 6000))
        router = build(plain, StubService())
        await router.start()
        await router.fetch_one("https://example.test/a")
        assert router.snapshot()["browser_started"] is False
        assert router.snapshot()["rendered"] is None


class TestLifecycle:
    async def test_close_does_not_start_an_unused_browser(self) -> None:
        plain, rendered = StubService(page(text="w" * 6000)), StubService()
        router = build(plain, rendered)
        await router.start()
        await router.close()

        assert plain.closed == 1
        assert rendered.started == 0
        assert rendered.closed == 0

    async def test_close_shuts_down_a_started_browser(self) -> None:
        plain = StubService(page(text="tiny"))
        rendered = StubService(page(text="R" * 6000, backend="browser"))
        router = build(plain, rendered)
        await router.start()
        await router.fetch_one("https://example.test/a")
        await router.close()

        assert rendered.closed == 1

    async def test_async_context_manager(self) -> None:
        plain, rendered = StubService(page(text="w" * 6000)), StubService()
        router = build(plain, rendered)
        async with router:
            assert router.ready
        assert plain.closed == 1

    async def test_concurrent_first_escalations_start_one_browser(self) -> None:
        plain = StubService(page(text="tiny"))
        rendered = StubService(page(text="R" * 6000, backend="browser"))
        router = build(plain, rendered, host_ttl_s=0)
        await router.start()

        await asyncio.gather(*(router.fetch_one(f"https://h{i}.test/a") for i in range(10)))
        assert rendered.started == 1
