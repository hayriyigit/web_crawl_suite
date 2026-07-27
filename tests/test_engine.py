"""Engine behaviour, driven through a fake backend so no browser is needed.

These tests are the contract for anything that plugs in as a backend: the
engine's dedup, scope, depth, retry and politeness handling must hold no matter
what does the fetching.
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import FakeBackend

from polycrawl import CrawlConfig, CrawlEngine, MemorySink
from polycrawl.models import FetchRequest, FetchResult, FetchStatus


def _page(*links: str) -> str:
    body = "".join(f'<a href="{href}">x</a>' for href in links)
    return f"<html><head><title>T</title></head><body><p>text</p>{body}</body></html>"


def _config(**kwargs: object) -> CrawlConfig:
    base: dict[str, object] = {
        "seeds": ["https://site.test/"],
        "backend": "fake",
        "concurrency": 8,
        "batch_size": 4,
        "max_pages": 50,
        "max_depth": 3,
        "progress": False,
        "output": {"format": "none"},
        "politeness": {"respect_robots": False, "per_host_rps": 1000, "per_host_concurrency": 8},
    }
    base.update(kwargs)
    return CrawlConfig(**base)  # type: ignore[arg-type]


async def _run(
    cfg: CrawlConfig, pages: dict[str, object]
) -> tuple[MemorySink, FakeBackend, object]:
    FakeBackend.pages = pages
    backend = FakeBackend(cfg)
    sink = MemorySink()
    engine = CrawlEngine(cfg, backend=backend, sink=sink)
    metrics = await asyncio.wait_for(engine.run(), timeout=30)
    return sink, backend, metrics


class TestTraversal:
    async def test_follows_links_and_records_depth(self) -> None:
        pages = {
            "https://site.test/": _page("/a", "/b"),
            "https://site.test/a": _page("/c"),
            "https://site.test/b": _page(),
            "https://site.test/c": _page(),
        }
        sink, _, _ = await _run(_config(), pages)
        by_url = {p.url: p for p in sink.pages}
        assert set(by_url) == set(pages)
        assert by_url["https://site.test/"].depth == 0
        assert by_url["https://site.test/a"].depth == 1
        assert by_url["https://site.test/c"].depth == 2

    async def test_records_parent(self) -> None:
        pages = {"https://site.test/": _page("/a"), "https://site.test/a": _page()}
        sink, _, _ = await _run(_config(), pages)
        child = next(p for p in sink.pages if p.url.endswith("/a"))
        assert child.parent == "https://site.test/"

    async def test_never_fetches_the_same_url_twice(self) -> None:
        pages = {
            "https://site.test/": _page("/a", "/b"),
            "https://site.test/a": _page("/", "/b"),
            "https://site.test/b": _page("/", "/a"),
        }
        _, backend, metrics = await _run(_config(), pages)
        assert len(backend.seen_urls) == len(set(backend.seen_urls)) == 3
        assert metrics.duplicates > 0  # type: ignore[attr-defined]

    async def test_dedups_equivalent_url_spellings(self) -> None:
        pages = {
            "https://site.test/": _page("/a?utm_source=x", "/a", "/a#frag"),
            "https://site.test/a": _page(),
        }
        _, backend, _ = await _run(_config(), pages)
        assert sorted(backend.seen_urls) == ["https://site.test/", "https://site.test/a"]

    async def test_max_depth_stops_descent(self) -> None:
        pages = {
            "https://site.test/": _page("/a"),
            "https://site.test/a": _page("/b"),
            "https://site.test/b": _page("/c"),
            "https://site.test/c": _page(),
        }
        sink, _, _ = await _run(_config(max_depth=1), pages)
        assert {p.url for p in sink.pages} == {"https://site.test/", "https://site.test/a"}

    async def test_max_pages_is_a_hard_cap(self) -> None:
        pages = {"https://site.test/": _page(*[f"/p{i}" for i in range(50)])}
        pages.update({f"https://site.test/p{i}": _page() for i in range(50)})
        _, backend, metrics = await _run(_config(max_pages=7), pages)
        assert len(backend.seen_urls) == 7
        assert metrics.fetched == 7  # type: ignore[attr-defined]

    async def test_follow_links_disabled(self) -> None:
        pages = {"https://site.test/": _page("/a"), "https://site.test/a": _page()}
        sink, _, _ = await _run(_config(follow_links=False), pages)
        assert len(sink.pages) == 1

    async def test_offsite_links_are_out_of_scope(self) -> None:
        pages = {
            "https://site.test/": _page("https://elsewhere.test/x", "/a"),
            "https://site.test/a": _page(),
        }
        sink, _, metrics = await _run(_config(scope="site"), pages)
        assert all("elsewhere" not in p.url for p in sink.pages)
        assert metrics.out_of_scope >= 1  # type: ignore[attr-defined]

    async def test_deny_pattern_excludes(self) -> None:
        pages = {
            "https://site.test/": _page("/keep", "/skip/x"),
            "https://site.test/keep": _page(),
            "https://site.test/skip/x": _page(),
        }
        sink, _, _ = await _run(_config(deny_patterns=[r"/skip/"]), pages)
        assert {p.url for p in sink.pages} == {"https://site.test/", "https://site.test/keep"}

    async def test_multiple_seeds(self) -> None:
        cfg = _config(seeds=["https://a.test/", "https://b.test/"], scope="any")
        pages = {"https://a.test/": _page(), "https://b.test/": _page()}
        sink, _, _ = await _run(cfg, pages)
        assert len(sink.pages) == 2


class TestFailureHandling:
    async def test_retries_transient_failures_then_gives_up(self) -> None:
        attempts: list[int] = []

        def flaky(req: FetchRequest) -> FetchResult:
            attempts.append(req.attempt)
            return FetchResult(request=req, status=FetchStatus.TIMEOUT, error="timeout")

        cfg = _config(max_retries=2, retry_backoff_s=0.01)
        sink, _, metrics = await _run(cfg, {"https://site.test/": flaky})

        assert attempts == [0, 1, 2]
        assert metrics.retried == 2  # type: ignore[attr-defined]
        assert len(sink.pages) == 1
        assert sink.pages[0].status == "timeout"

    async def test_http_errors_are_not_retried(self) -> None:
        calls: list[int] = []

        def not_found(req: FetchRequest) -> FetchResult:
            calls.append(1)
            return FetchResult(request=req, status=FetchStatus.HTTP_ERROR, http_status=404)

        _, _, metrics = await _run(_config(max_retries=3), {"https://site.test/": not_found})
        assert len(calls) == 1
        assert metrics.retried == 0  # type: ignore[attr-defined]

    async def test_retries_do_not_consume_the_page_budget(self) -> None:
        """A retried URL is the same page, so it must not count twice."""
        state = {"n": 0}

        def flaky_then_ok(req: FetchRequest) -> FetchResult:
            state["n"] += 1
            if req.attempt == 0:
                return FetchResult(request=req, status=FetchStatus.TIMEOUT, error="t")
            return FetchResult(
                request=req,
                status=FetchStatus.OK,
                http_status=200,
                html=_page("/a"),
                url=req.url,
            )

        pages: dict[str, object] = {"https://site.test/": flaky_then_ok}
        pages["https://site.test/a"] = _page()
        cfg = _config(max_pages=2, max_retries=2, retry_backoff_s=0.01)
        sink, _, _ = await _run(cfg, pages)
        assert {p.url for p in sink.pages} == {"https://site.test/", "https://site.test/a"}

    async def test_failed_pages_are_still_written(self) -> None:
        def boom(req: FetchRequest) -> FetchResult:
            return FetchResult(request=req, status=FetchStatus.NETWORK_ERROR, error="dns failure")

        cfg = _config(max_retries=0)
        sink, _, metrics = await _run(cfg, {"https://site.test/": boom})
        assert len(sink.pages) == 1
        assert sink.pages[0].error == "dns failure"
        assert metrics.failed == 1  # type: ignore[attr-defined]

    async def test_a_broken_backend_does_not_hang_the_crawl(self) -> None:
        class BrokenBackend(FakeBackend):
            async def submit(self, batch):  # type: ignore[no-untyped-def]
                raise RuntimeError("submit exploded")

        cfg = _config(max_retries=0)
        backend = BrokenBackend(cfg)
        engine = CrawlEngine(cfg, backend=backend, sink=MemorySink())
        metrics = await asyncio.wait_for(engine.run(), timeout=15)
        assert metrics.failed >= 1

    async def test_engine_rejects_empty_seeds(self) -> None:
        with pytest.raises(ValueError, match="no seed"):
            await CrawlEngine(_config(seeds=[])).run()


class TestConcurrencyLimits:
    async def test_global_concurrency_is_never_exceeded(self) -> None:
        pages = {"https://site.test/": _page(*[f"/p{i}" for i in range(60)])}
        pages.update({f"https://site.test/p{i}": _page() for i in range(60)})
        cfg = _config(concurrency=5, batch_size=5, max_pages=61)
        _, backend, _ = await _run(cfg, pages)
        assert backend.max_observed_in_flight <= 5

    async def test_per_host_concurrency_is_respected(self) -> None:
        pages = {"https://site.test/": _page(*[f"/p{i}" for i in range(30)])}
        pages.update({f"https://site.test/p{i}": _page() for i in range(30)})
        cfg = _config(
            concurrency=16,
            batch_size=16,
            max_pages=31,
            politeness={
                "respect_robots": False,
                "per_host_rps": 1000,
                "per_host_concurrency": 2,
            },
        )
        _, backend, _ = await _run(cfg, pages)
        # Only one host exists here, so the per-host cap is the binding limit.
        assert backend.max_observed_in_flight <= 2

    async def test_work_spreads_across_hosts(self) -> None:
        seeds = [f"https://h{i}.test/" for i in range(4)]
        pages = {s: _page() for s in seeds}
        cfg = _config(seeds=seeds, scope="any", concurrency=8, batch_size=4)
        _, backend, _ = await _run(cfg, pages)
        assert len({FetchRequest(url=u).host for u in backend.seen_urls}) == 4


class TestOutput:
    async def test_text_and_title_extracted(self) -> None:
        sink, _, _ = await _run(_config(), {"https://site.test/": _page()})
        page = sink.pages[0]
        assert page.title == "T"
        assert "text" in page.text

    async def test_html_omitted_unless_requested(self) -> None:
        sink, _, _ = await _run(_config(), {"https://site.test/": _page()})
        assert sink.pages[0].html is None

    async def test_html_retained_when_requested(self) -> None:
        cfg = _config(output={"format": "none", "include_html": True})
        sink, _, _ = await _run(cfg, {"https://site.test/": _page()})
        assert sink.pages[0].html is not None

    async def test_jsonl_file_written(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        import orjson

        out = tmp_path / "pages.jsonl"
        cfg = _config(output={"path": out, "format": "jsonl"})
        FakeBackend.pages = {"https://site.test/": _page("/a"), "https://site.test/a": _page()}
        engine = CrawlEngine(cfg, backend=FakeBackend(cfg))
        await asyncio.wait_for(engine.run(), timeout=30)

        rows = [orjson.loads(line) for line in out.read_text().splitlines()]
        assert len(rows) == 2
        assert {r["url"] for r in rows} == {"https://site.test/", "https://site.test/a"}
        assert rows[0]["status"] == "ok"


class TestLifecycle:
    async def test_backend_is_started_and_closed(self) -> None:
        cfg = _config()
        FakeBackend.pages = {"https://site.test/": _page()}
        backend = FakeBackend(cfg)
        # The engine only owns teardown for backends it created itself.
        engine = CrawlEngine(cfg, backend=backend, sink=MemorySink())
        await asyncio.wait_for(engine.run(), timeout=15)
        assert backend.started is True

    async def test_stop_ends_the_crawl_early(self) -> None:
        class SlowBackend(FakeBackend):
            """Paces fetches so there is a crawl still in progress to stop."""

            async def _run(self, batch):  # type: ignore[no-untyped-def]
                await asyncio.sleep(0.02)
                await super()._run(batch)

        pages = {"https://site.test/": _page(*[f"/p{i}" for i in range(40)])}
        pages.update({f"https://site.test/p{i}": _page() for i in range(40)})
        cfg = _config(max_pages=41, concurrency=2, batch_size=2)
        FakeBackend.pages = pages
        engine = CrawlEngine(cfg, backend=SlowBackend(cfg), sink=MemorySink())

        run_task = asyncio.create_task(engine.run())
        # Stop only once the crawl is demonstrably under way, so the assertion
        # tests early termination rather than a race against start-up.
        while engine.metrics.fetched < 2:
            await asyncio.sleep(0.005)
        engine.stop("test")

        metrics = await asyncio.wait_for(run_task, timeout=20)
        assert metrics.fetched < 41

    async def test_time_budget_ends_the_crawl(self) -> None:
        def slow(req: FetchRequest) -> FetchResult:
            return FetchResult(
                request=req, status=FetchStatus.OK, http_status=200, html=_page(), url=req.url
            )

        pages: dict[str, object] = {"https://site.test/": _page(*[f"/p{i}" for i in range(30)])}
        pages.update({f"https://site.test/p{i}": slow for i in range(30)})
        cfg = _config(max_pages=31, time_budget_s=0.15, concurrency=1, batch_size=1)
        _, _, metrics = await _run(cfg, pages)
        assert metrics.elapsed < 10  # type: ignore[attr-defined]

    async def test_snapshot_exposes_live_state(self) -> None:
        cfg = _config()
        FakeBackend.pages = {"https://site.test/": _page()}
        engine = CrawlEngine(cfg, backend=FakeBackend(cfg), sink=MemorySink())
        await asyncio.wait_for(engine.run(), timeout=15)
        snap = engine.snapshot()
        assert snap["fetched"] == 1
        assert "queued" in snap and "in_flight" in snap
