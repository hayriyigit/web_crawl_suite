"""End-to-end tests against a local JS-rendered site, run on every backend.

The site's links and body text are injected by JavaScript at DOMContentLoaded,
so these tests fail for any backend that does not actually render. They are
parametrised over the backends so both implementations are held to the same
observable behaviour -- that equivalence is the point of the architecture.
"""

from __future__ import annotations

import asyncio

import pytest
from server import PAGE_COUNT, LocalSite

from polycrawl import CrawlConfig, CrawlEngine, MemorySink
from polycrawl.registry import get_backend

BACKENDS = ["crawl4ai", "crawlee", "scrapy"]


def _available(name: str) -> bool:
    try:
        return get_backend(name).is_available()[0]
    except Exception:
        return False


pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def site():  # type: ignore[no-untyped-def]
    with LocalSite() as s:
        yield s


def _config(site, backend: str, **kwargs):  # type: ignore[no-untyped-def]
    base = {
        "seeds": [site.base_url],
        "backend": backend,
        "concurrency": 6,
        "batch_size": 4,
        "max_pages": PAGE_COUNT,
        "max_depth": 4,
        "scope": "host",
        "progress": False,
        "output": {"format": "none"},
        "politeness": {
            "respect_robots": False,
            "per_host_rps": 100,
            "per_host_concurrency": 6,
        },
    }
    base.update(kwargs)
    return CrawlConfig(**base)


async def _crawl(cfg: CrawlConfig, timeout: float = 180) -> tuple[MemorySink, object]:
    sink = MemorySink()
    engine = CrawlEngine(cfg, sink=sink)
    metrics = await asyncio.wait_for(engine.run(), timeout=timeout)
    return sink, metrics


@pytest.mark.parametrize("backend", BACKENDS)
class TestBackendParity:
    async def test_javascript_is_executed(self, site, backend: str) -> None:  # type: ignore[no-untyped-def]
        if not _available(backend):
            pytest.skip(f"{backend} not installed")
        cfg = _config(site, backend, max_pages=1, max_depth=0)
        sink, _ = await _crawl(cfg)

        page = sink.pages[0]
        # The server ships "Static Title 0"; JS rewrites it to "JS Title 0".
        assert page.title == "JS Title 0", "backend did not execute JavaScript"
        assert "marker-js-rendered-0" in page.text
        assert "Rendered Page 0" in page.text

    async def test_follows_javascript_injected_links(self, site, backend: str) -> None:  # type: ignore[no-untyped-def]
        if not _available(backend):
            pytest.skip(f"{backend} not installed")
        sink, _ = await _crawl(_config(site, backend))

        # Links exist only in the DOM after JS runs, so any depth > 0 page
        # proves the backend both rendered and handed back usable HTML.
        assert max(p.depth for p in sink.pages) >= 2
        assert len(sink.pages) >= 6
        assert all("marker-js-rendered" in p.text for p in sink.pages)

    async def test_no_duplicate_fetches(self, site, backend: str) -> None:  # type: ignore[no-untyped-def]
        if not _available(backend):
            pytest.skip(f"{backend} not installed")
        sink, _ = await _crawl(_config(site, backend))
        urls = [p.url for p in sink.pages]
        assert len(urls) == len(set(urls))

    async def test_max_pages_is_enforced(self, site, backend: str) -> None:  # type: ignore[no-untyped-def]
        if not _available(backend):
            pytest.skip(f"{backend} not installed")
        sink, metrics = await _crawl(_config(site, backend, max_pages=3))
        assert len(sink.pages) == 3
        assert metrics.fetched == 3  # type: ignore[attr-defined]

    async def test_robots_txt_is_obeyed(self, site, backend: str) -> None:  # type: ignore[no-untyped-def]
        if not _available(backend):
            pytest.skip(f"{backend} not installed")
        cfg = _config(
            site,
            backend,
            seeds=[f"{site.base_url}/private/secret"],
            max_pages=2,
            politeness={"respect_robots": True, "per_host_rps": 100},
        )
        sink, metrics = await _crawl(cfg)
        assert metrics.robots_blocked == 1  # type: ignore[attr-defined]
        assert sink.pages == []

    async def test_http_errors_are_reported_not_crashed_on(self, site, backend: str) -> None:  # type: ignore[no-untyped-def]
        if not _available(backend):
            pytest.skip(f"{backend} not installed")
        cfg = _config(
            site, backend, seeds=[f"{site.base_url}/status/500"], max_pages=1, max_depth=0
        )
        sink, _ = await _crawl(cfg)
        assert len(sink.pages) == 1
        assert sink.pages[0].http_status == 500

    async def test_jsonl_output(self, site, backend: str, tmp_path) -> None:  # type: ignore[no-untyped-def]
        if not _available(backend):
            pytest.skip(f"{backend} not installed")
        import orjson

        out = tmp_path / f"{backend}.jsonl"
        cfg = _config(site, backend, max_pages=4, output={"path": out, "format": "jsonl"})
        engine = CrawlEngine(cfg)
        await asyncio.wait_for(engine.run(), timeout=180)

        rows = [orjson.loads(x) for x in out.read_text().splitlines()]
        assert len(rows) == 4
        assert all(r["backend"] == backend for r in rows)
        assert all(r["title"].startswith("JS Title") for r in rows)


class TestCrossBackendEquivalence:
    """Every backend must agree on what a crawl produces.

    This is the property the whole architecture exists to provide: if two
    backends disagree about a crawl, then choosing one is not an implementation
    detail and they are not interchangeable.
    """

    def _all_or_skip(self) -> None:
        missing = [n for n in BACKENDS if not _available(n)]
        if missing:
            pytest.skip(f"not installed: {', '.join(missing)}")

    async def test_same_pages_discovered(self, site) -> None:  # type: ignore[no-untyped-def]
        self._all_or_skip()

        results = {}
        for name in BACKENDS:
            # The page budget must be high enough to exhaust the frontier. A
            # crawl cut short by max_pages keeps whichever URLs happened to be
            # discovered first, which is a function of fetch latency -- so a
            # truncated crawl cannot be compared across backends at all.
            sink, metrics = await _crawl(_config(site, name, max_pages=100))
            results[name] = ({p.url for p in sink.pages}, metrics)

        reference = BACKENDS[0]
        urls_a, metrics_a = results[reference]
        assert len(urls_a) > PAGE_COUNT, "expected the crawl to exhaust the frontier"
        for name in BACKENDS[1:]:
            urls_b, metrics_b = results[name]
            assert urls_b == urls_a, f"{name} discovered a different set than {reference}"
            assert metrics_b.succeeded == metrics_a.succeeded  # type: ignore[attr-defined]
            assert metrics_b.out_of_scope == metrics_a.out_of_scope  # type: ignore[attr-defined]

    async def test_same_extracted_text(self, site) -> None:  # type: ignore[no-untyped-def]
        self._all_or_skip()

        texts = {}
        for name in BACKENDS:
            cfg = _config(site, name, max_pages=1, max_depth=0)
            sink, _ = await _crawl(cfg)
            texts[name] = (sink.pages[0].title, sink.pages[0].text)

        # Central extraction means identical HTML yields identical fields, so
        # these are compared exactly rather than loosely.
        reference = BACKENDS[0]
        for name in BACKENDS[1:]:
            assert texts[name] == texts[reference], f"{name} extracted different text"


class TestScrapyHtmlOnlyMode:
    """``render=False`` trades JavaScript for speed; prove it really is the trade."""

    async def test_html_only_mode_does_not_render(self, site) -> None:  # type: ignore[no-untyped-def]
        if not _available("scrapy"):
            pytest.skip("scrapy not installed")
        cfg = _config(
            site,
            "scrapy",
            max_pages=1,
            max_depth=0,
            backend_options={"render": False},
        )
        sink, _ = await _crawl(cfg)

        page = sink.pages[0]
        # The server ships this title and JS rewrites it; without a browser the
        # original survives, which is what makes this mode measurably faster.
        assert page.title == "Static Title 0"
        assert "marker-js-rendered-0" not in page.text

    async def test_html_only_mode_finds_no_javascript_links(self, site) -> None:  # type: ignore[no-untyped-def]
        if not _available("scrapy"):
            pytest.skip("scrapy not installed")
        cfg = _config(site, "scrapy", backend_options={"render": False})
        sink, _ = await _crawl(cfg)

        # Every link on the test site is injected by JS, so an unrendered crawl
        # cannot get past the seed.
        assert len(sink.pages) == 1

    async def test_html_only_mode_crawls_server_rendered_links(self, site) -> None:  # type: ignore[no-untyped-def]
        """Without a browser it is still a crawler, given links in the HTML."""
        if not _available("scrapy"):
            pytest.skip("scrapy not installed")
        cfg = _config(
            site,
            "scrapy",
            seeds=[f"{site.base_url}/static/0"],
            max_pages=100,
            backend_options={"render": False},
        )
        sink, _ = await _crawl(cfg)

        assert len(sink.pages) >= 6
        assert max(p.depth for p in sink.pages) >= 2
        assert all("marker-server-rendered" in p.text for p in sink.pages)
