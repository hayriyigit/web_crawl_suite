"""Unit tests for the frontier, dedup, rate limiter, extractor and sinks."""

from __future__ import annotations

import time
from pathlib import Path

import orjson
import pytest

from polycrawl.dedup import BloomSeen, HashSetSeen, make_seen_set
from polycrawl.extract import extract_links, extract_page
from polycrawl.frontier import Frontier
from polycrawl.models import CrawledPage, FetchRequest
from polycrawl.ratelimit import HostLimiter
from polycrawl.sinks import JsonlSink


class TestDedup:
    def test_hashset_reports_novelty(self) -> None:
        seen = HashSetSeen()
        assert seen.add("https://a.com/") is True
        assert seen.add("https://a.com/") is False
        assert "https://a.com/" in seen
        assert len(seen) == 1

    def test_bloom_has_no_false_negatives(self) -> None:
        bloom = BloomSeen(capacity=5_000, error_rate=0.01)
        urls = [f"https://example.com/{i}" for i in range(2_000)]
        for url in urls:
            bloom.add(url)
        # A bloom filter may claim it has seen something it hasn't, but it must
        # never forget something it has -- that is the property crawling relies
        # on to avoid refetch loops.
        assert all(u in bloom for u in urls)

    def test_bloom_false_positive_rate_is_near_target(self) -> None:
        bloom = BloomSeen(capacity=10_000, error_rate=0.01)
        for i in range(10_000):
            bloom.add(f"https://example.com/seen/{i}")
        probes = [f"https://example.com/unseen/{i}" for i in range(5_000)]
        fps = sum(1 for p in probes if p in bloom)
        assert fps / len(probes) < 0.05

    def test_bloom_memory_is_bounded(self) -> None:
        bloom = BloomSeen(capacity=1_000_000, error_rate=0.001)
        before = bloom.nbytes
        for i in range(10_000):
            bloom.add(f"https://x.com/{i}")
        assert bloom.nbytes == before
        assert bloom.nbytes < 2_000_000

    def test_factory(self) -> None:
        assert isinstance(make_seen_set("exact"), HashSetSeen)
        assert isinstance(make_seen_set("bloom", capacity=1000), BloomSeen)
        with pytest.raises(ValueError, match="unknown dedup kind"):
            make_seen_set("nope")


class TestFrontier:
    def test_spreads_batch_across_hosts(self) -> None:
        """The central heavy-load property: a batch must not be one host's run."""
        f = Frontier()
        for host in ("a.com", "b.com", "c.com"):
            for i in range(10):
                f.push(FetchRequest(url=f"https://{host}/{i}"))

        batch = f.pop_batch(3)
        hosts = {r.host for r in batch}
        assert len(batch) == 3
        assert hosts == {"a.com", "b.com", "c.com"}

    def test_skips_unready_hosts_without_losing_them(self) -> None:
        f = Frontier()
        f.push(FetchRequest(url="https://blocked.com/1"))
        f.push(FetchRequest(url="https://open.com/1"))

        batch = f.pop_batch(5, is_ready=lambda h: h != "blocked.com", ready_at=lambda h: 0.0)
        assert [r.host for r in batch] == ["open.com"]
        assert len(f) == 1

        # Once ready, the deferred host comes back.
        batch2 = f.pop_batch(5, is_ready=lambda h: True, ready_at=lambda h: 0.0)
        assert [r.host for r in batch2] == ["blocked.com"]

    def test_does_not_spin_when_every_host_is_throttled(self) -> None:
        f = Frontier()
        for i in range(50):
            f.push(FetchRequest(url=f"https://h{i}.com/x"))
        started = time.monotonic()
        assert f.pop_batch(10, is_ready=lambda h: False, ready_at=lambda h: 1e9) == []
        assert time.monotonic() - started < 1.0

    def test_respects_global_bound(self) -> None:
        f = Frontier(max_size=5)
        pushed = sum(f.push(FetchRequest(url=f"https://a.com/{i}")) for i in range(10))
        assert pushed == 5
        assert f.dropped == 5

    def test_respects_per_host_bound(self) -> None:
        f = Frontier(max_per_host=3)
        for i in range(10):
            f.push(FetchRequest(url=f"https://a.com/{i}"))
        for i in range(3):
            f.push(FetchRequest(url=f"https://b.com/{i}"))
        assert len(f) == 6

    def test_front_push_is_taken_first(self) -> None:
        f = Frontier()
        f.push(FetchRequest(url="https://a.com/normal"))
        f.push(FetchRequest(url="https://a.com/retry"), front=True)
        assert f.pop_batch(1)[0].url.endswith("/retry")


class TestHostLimiter:
    def test_burst_then_throttle(self) -> None:
        limiter = HostLimiter(base_rate=1.0, burst=3.0)
        grants = [limiter.try_acquire("a.com") for _ in range(6)]
        assert sum(grants) <= 3
        assert grants[0] is True

    def test_hosts_are_independent(self) -> None:
        limiter = HostLimiter(base_rate=1.0, burst=1.0)
        assert limiter.try_acquire("a.com") is True
        assert limiter.try_acquire("b.com") is True

    def test_throttling_halves_the_rate_and_pauses(self) -> None:
        limiter = HostLimiter(base_rate=8.0, burst=8.0)
        before = limiter.bucket("a.com").rate
        limiter.on_throttled("a.com", retry_after=0.5)
        after = limiter.bucket("a.com")
        assert after.rate == pytest.approx(before / 2)
        assert after.blocked_until > time.monotonic()
        assert limiter.try_acquire("a.com") is False

    def test_retry_after_is_honoured(self) -> None:
        limiter = HostLimiter()
        limiter.on_throttled("a.com", retry_after=30.0)
        assert limiter.ready_at("a.com") >= time.monotonic() + 29

    def test_success_streak_raises_rate(self) -> None:
        limiter = HostLimiter(base_rate=1.0, max_rate=10.0, increase_step=0.5)
        start = limiter.bucket("a.com").rate
        for _ in range(10):
            limiter.on_success("a.com")
        assert limiter.bucket("a.com").rate > start

    def test_crawl_delay_caps_rate(self) -> None:
        limiter = HostLimiter(base_rate=10.0)
        limiter.set_crawl_delay("a.com", 5.0)
        assert limiter.bucket("a.com").rate == pytest.approx(0.2)

    def test_rate_never_drops_below_floor(self) -> None:
        limiter = HostLimiter(base_rate=4.0, min_rate=0.5)
        for _ in range(20):
            limiter.on_throttled("a.com", retry_after=0)
        assert limiter.bucket("a.com").rate >= 0.5


class TestExtract:
    HTML = """
    <html><head>
      <title>  Test Page </title>
      <meta name="description" content="a description">
      <link rel="canonical" href="/canonical">
    </head><body>
      <p>Visible text.</p>
      <script>var secret = 'should-not-appear';</script>
      <style>.x { color: red }</style>
      <a href="/rel">rel</a>
      <a href="https://other.com/abs">abs</a>
      <a href="#frag">frag</a>
      <a href="javascript:void(0)">js</a>
      <a href="/spam" rel="nofollow">nofollow</a>
      <a href="/rel">dupe</a>
    </body></html>
    """

    def test_links_are_absolute_normalised_and_unique(self) -> None:
        links = extract_links(self.HTML, "https://example.com/page")
        assert "https://example.com/rel" in links
        assert "https://other.com/abs" in links
        assert len(links) == len(set(links))
        # Fragments, javascript: and rel=nofollow are all excluded.
        assert not any("frag" in x or "javascript" in x or "spam" in x for x in links)

    def test_base_href_changes_resolution(self) -> None:
        html = (
            '<html><head><base href="https://cdn.example.com/x/"></head>'
            '<body><a href="y">y</a></body></html>'
        )
        assert extract_links(html, "https://example.com/page") == ["https://cdn.example.com/x/y"]

    def test_text_excludes_script_and_style_bodies(self) -> None:
        page = extract_page(self.HTML, "https://example.com/page")
        assert "Visible text." in page.text
        assert "should-not-appear" not in page.text
        assert "color: red" not in page.text

    def test_metadata(self) -> None:
        page = extract_page(self.HTML, "https://example.com/page")
        assert page.title == "Test Page"
        assert page.meta_description == "a description"
        assert page.canonical == "https://example.com/canonical"

    def test_meta_robots_nofollow_suppresses_links(self) -> None:
        html = (
            '<html><head><meta name="robots" content="nofollow"></head>'
            '<body><a href="/a">a</a></body></html>'
        )
        page = extract_page(html, "https://example.com/")
        assert page.nofollow is True
        assert page.links == []

    def test_text_truncation(self) -> None:
        html = "<html><body>" + ("word " * 5000) + "</body></html>"
        page = extract_page(html, "https://e.com/", max_text_chars=100)
        assert len(page.text) == 100

    def test_malformed_html_does_not_raise(self) -> None:
        page = extract_page("<html><body><a href=", "https://e.com/")
        assert isinstance(page.links, list)

    def test_empty_html(self) -> None:
        assert extract_links("", "https://e.com/") == []


class TestJsonlSink:
    @staticmethod
    def _page(url: str) -> CrawledPage:
        return CrawledPage(
            url=url,
            final_url=url,
            depth=0,
            parent=None,
            http_status=200,
            status="ok",
            title="t",
            text="body",
            html="<html></html>",
            links=[],
            n_links=0,
            error=None,
            elapsed=0.1,
            backend="fake",
            fetched_at=1.0,
        )

    async def test_writes_one_json_object_per_line(self, tmp_path: Path) -> None:
        out = tmp_path / "o.jsonl"
        async with JsonlSink(out, buffer_bytes=16) as sink:
            for i in range(5):
                await sink.write(self._page(f"https://e.com/{i}"))

        lines = out.read_text().strip().splitlines()
        assert len(lines) == 5
        assert orjson.loads(lines[0])["url"] == "https://e.com/0"

    async def test_html_excluded_by_default(self, tmp_path: Path) -> None:
        out = tmp_path / "o.jsonl"
        async with JsonlSink(out) as sink:
            await sink.write(self._page("https://e.com/1"))
        assert "html" not in orjson.loads(out.read_text().strip())

    async def test_html_included_on_request(self, tmp_path: Path) -> None:
        out = tmp_path / "o.jsonl"
        async with JsonlSink(out, include_html=True) as sink:
            await sink.write(self._page("https://e.com/1"))
        assert orjson.loads(out.read_text().strip())["html"] == "<html></html>"

    async def test_gzip_round_trip(self, tmp_path: Path) -> None:
        import gzip

        out = tmp_path / "o.jsonl.gz"
        async with JsonlSink(out, gzip_output=True) as sink:
            await sink.write(self._page("https://e.com/1"))
        with gzip.open(out, "rb") as fh:
            assert orjson.loads(fh.read().strip())["url"] == "https://e.com/1"

    async def test_buffer_flushes_on_close_not_only_on_threshold(self, tmp_path: Path) -> None:
        out = tmp_path / "o.jsonl"
        sink = JsonlSink(out, buffer_bytes=10_000_000)
        await sink.open()
        await sink.write(self._page("https://e.com/1"))
        await sink.close()
        assert out.read_text().strip() != ""
