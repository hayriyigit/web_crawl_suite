"""FetchService: the request/response path, driven without a browser.

These are the guarantees an API in front of this depends on -- every URL comes
back, one caller's bad host does not sink another's batch, capacity is bounded,
and politeness is shared rather than per caller.
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import FakeBackend

from polycrawl import CrawlConfig, FetchService, ServiceBusy
from polycrawl.models import FetchRequest, FetchResult, FetchStatus


def _page(title: str = "T", body: str = "hello") -> str:
    return f"<html><head><title>{title}</title></head><body><p>{body}</p></body></html>"


def _config(**kwargs: object) -> CrawlConfig:
    base: dict[str, object] = {
        "seeds": ["https://site.test/"],
        "backend": "fake",
        "concurrency": 8,
        "max_pages": 1,
        "progress": False,
        "output": {"format": "none"},
        "politeness": {
            "respect_robots": False,
            "per_host_rps": 1000,
            "per_host_concurrency": 8,
        },
    }
    base.update(kwargs)
    return CrawlConfig(**base)  # type: ignore[arg-type]


async def _service(pages: dict[str, object], **kwargs: object) -> FetchService:
    FakeBackend.pages = pages
    cfg = _config(**{k: v for k, v in kwargs.items() if k in _CONFIG_KEYS})
    service = FetchService(
        cfg,
        backend=FakeBackend(cfg),
        **{k: v for k, v in kwargs.items() if k not in _CONFIG_KEYS},  # type: ignore[arg-type]
    )
    await service.start()
    return service


_CONFIG_KEYS = {"concurrency", "politeness", "output", "max_retries", "retry_backoff_s"}


class TestBasics:
    async def test_returns_a_page_per_url_in_order(self) -> None:
        pages = {
            "https://site.test/a": _page("A"),
            "https://site.test/b": _page("B"),
            "https://site.test/c": _page("C"),
        }
        service = await _service(pages)
        try:
            urls = ["https://site.test/c", "https://site.test/a", "https://site.test/b"]
            got = await service.fetch(urls)
            assert [p.title for p in got] == ["C", "A", "B"]
            assert all(p.status == "ok" for p in got)
        finally:
            await service.close()

    async def test_extracts_text_and_links(self) -> None:
        html = (
            '<html><head><title>T</title></head><body><p>words</p><a href="/x">x</a></body></html>'
        )
        service = await _service({"https://site.test/": html})
        try:
            page = await service.fetch_one("https://site.test/")
            assert "words" in page.text
            assert page.links == ["https://site.test/x"]
        finally:
            await service.close()

    async def test_one_bad_url_does_not_sink_the_batch(self) -> None:
        """A caller must learn which of its URLs failed, not lose all of them."""

        def boom(req: FetchRequest) -> FetchResult:
            return FetchResult(request=req, status=FetchStatus.NETWORK_ERROR, error="dns")

        pages: dict[str, object] = {
            "https://site.test/ok": _page("OK"),
            "https://site.test/bad": boom,
        }
        service = await _service(pages, max_retries=0)
        try:
            got = await service.fetch(["https://site.test/ok", "https://site.test/bad"])
            assert got[0].status == "ok"
            assert got[1].status == "network_error"
            assert got[1].error is not None
        finally:
            await service.close()

    async def test_unusable_url_is_reported_not_raised(self) -> None:
        service = await _service({})
        try:
            page = await service.fetch_one("not a url at all")
            assert page.error is not None
        finally:
            await service.close()

    async def test_a_second_caller_may_refetch_the_same_url(self) -> None:
        """Unlike a crawl, dedup must NOT apply across callers."""
        service = await _service({"https://site.test/": _page("T")})
        try:
            first = await service.fetch_one("https://site.test/")
            second = await service.fetch_one("https://site.test/")
            assert first.status == second.status == "ok"
            assert service.stats.completed == 2
        finally:
            await service.close()


class TestCapacity:
    async def test_rejects_when_full_rather_than_queueing_forever(self) -> None:
        """Fail fast: an unbounded queue turns slowness into timeouts for all."""
        release = asyncio.Event()

        class SlowBackend(FakeBackend):
            async def _run(self, batch: list[FetchRequest]) -> None:  # type: ignore[override]
                await release.wait()
                for req in batch:
                    await self.emit(self._make(req))

        cfg = _config(concurrency=1)
        SlowBackend.pages = {f"https://site.test/{i}": _page() for i in range(4)}
        service = FetchService(cfg, backend=SlowBackend(cfg), queue_timeout=0.05)
        await service.start()
        try:
            first = asyncio.create_task(service.fetch_one("https://site.test/0"))
            await asyncio.sleep(0.05)  # let it take the only slot
            with pytest.raises(ServiceBusy):
                await service.fetch_one("https://site.test/1")
            assert service.stats.rejected == 1
            release.set()
            await asyncio.wait_for(first, timeout=5)
        finally:
            release.set()
            await service.close()

    async def test_a_hung_host_times_out_instead_of_hanging_the_caller(self) -> None:
        class NeverAnswers(FakeBackend):
            async def _run(self, batch: list[FetchRequest]) -> None:  # type: ignore[override]
                await asyncio.sleep(30)

        cfg = _config()
        service = FetchService(cfg, backend=NeverAnswers(cfg), request_timeout=0.1, max_retries=0)
        await service.start()
        try:
            page = await asyncio.wait_for(service.fetch_one("https://site.test/"), timeout=5)
            assert page.status == "timeout"
            assert service.stats.timed_out == 1
        finally:
            await service.close()

    async def test_concurrency_is_capped_across_callers(self) -> None:
        pages = {f"https://site.test/{i}": _page() for i in range(20)}
        service = await _service(pages, concurrency=3)
        backend = service._backend
        try:
            await service.fetch([f"https://site.test/{i}" for i in range(20)])
            assert isinstance(backend, FakeBackend)
            assert backend.max_observed_in_flight <= 3
        finally:
            await service.close()


class TestRetries:
    async def test_transient_failure_is_retried_then_succeeds(self) -> None:
        attempts: list[int] = []

        def flaky(req: FetchRequest) -> FetchResult:
            attempts.append(req.attempt)
            if req.attempt == 0:
                return FetchResult(request=req, status=FetchStatus.TIMEOUT, error="t")
            return FetchResult(
                request=req,
                status=FetchStatus.OK,
                url=req.url,
                http_status=200,
                html=_page("Second"),
            )

        service = await _service({"https://site.test/": flaky}, max_retries=2, retry_backoff_s=0.0)
        try:
            page = await service.fetch_one("https://site.test/")
            assert page.status == "ok"
            assert page.title == "Second"
            assert attempts == [0, 1]
            assert service.stats.retried == 1
        finally:
            await service.close()

    async def test_http_errors_are_not_retried(self) -> None:
        calls: list[int] = []

        def not_found(req: FetchRequest) -> FetchResult:
            calls.append(1)
            return FetchResult(request=req, status=FetchStatus.HTTP_ERROR, http_status=404)

        service = await _service({"https://site.test/": not_found}, max_retries=3)
        try:
            page = await service.fetch_one("https://site.test/")
            assert page.http_status == 404
            assert len(calls) == 1
        finally:
            await service.close()


class TestPoliteness:
    async def test_per_host_rate_is_shared_between_callers(self) -> None:
        """Politeness is a property of the service, not of one request."""
        pages = {f"https://slow.test/{i}": _page() for i in range(4)}
        service = await _service(
            pages,
            politeness={
                "respect_robots": False,
                "per_host_rps": 5.0,
                "per_host_burst": 1.0,
                "per_host_concurrency": 4,
            },
        )
        try:
            loop = asyncio.get_running_loop()
            start = loop.time()
            # Two separate callers, same host: the limiter must pace them together.
            await asyncio.gather(
                service.fetch([f"https://slow.test/{i}" for i in range(2)]),
                service.fetch([f"https://slow.test/{i}" for i in range(2, 4)]),
            )
            elapsed = loop.time() - start
            # 4 requests, 1 burst token, 5/s => at least ~3 refills.
            assert elapsed >= 0.4, f"limiter did not pace concurrent callers ({elapsed:.2f}s)"
        finally:
            await service.close()

    async def test_snapshot_reports_health(self) -> None:
        service = await _service({"https://site.test/": _page()})
        try:
            await service.fetch_one("https://site.test/")
            snap = service.snapshot()
            assert snap["completed"] == 1
            assert snap["in_flight"] == 0
            assert snap["backend"] == "fake"
            assert "uptime_s" in snap
        finally:
            await service.close()


class TestLifecycle:
    async def test_fetch_before_start_is_an_error(self) -> None:
        cfg = _config()
        service = FetchService(cfg, backend=FakeBackend(cfg))
        with pytest.raises(RuntimeError, match="start"):
            await service.fetch_one("https://site.test/")

    async def test_context_manager_starts_and_closes(self) -> None:
        FakeBackend.pages = {"https://site.test/": _page()}
        cfg = _config()
        backend = FakeBackend(cfg)
        async with FetchService(cfg, backend=backend) as service:
            assert service.ready
            await service.fetch_one("https://site.test/")
        assert not service.ready
