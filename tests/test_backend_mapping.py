"""How each backend maps its library's outcome onto a FetchResult.

These run without a browser: the mapping is a pure function of whatever the
underlying library handed back, so the library's response is stubbed. The point
is that no backend gets to invent its own idea of what a failed crawl is --
that decision belongs to the engine, and it has to look the same everywhere.
"""

from __future__ import annotations

from typing import Any

from polycrawl import CrawlConfig
from polycrawl.backends.crawl4ai_backend import Crawl4AIBackend
from polycrawl.models import FetchRequest, FetchStatus


class FakeCrawl4AIResult:
    """Stands in for crawl4ai's CrawlResult."""

    def __init__(self, **kwargs: Any) -> None:
        self.success = kwargs.get("success", True)
        self.status_code = kwargs.get("status_code", 200)
        self.error_message = kwargs.get("error_message")
        self.html = kwargs.get("html", "<html><body><p>hello</p></body></html>")
        self.url = kwargs.get("url", "https://site.test/")
        self.redirected_url = kwargs.get("redirected_url")


def _map(**kwargs: Any):  # type: ignore[no-untyped-def]
    backend = Crawl4AIBackend(CrawlConfig(seeds=["https://site.test/"]))
    request = FetchRequest(url="https://site.test/")
    return backend._to_result(request, FakeCrawl4AIResult(**kwargs), started=0.0)


class TestCrawl4AIOutcomeMapping:
    def test_success_is_ok(self) -> None:
        assert _map().status is FetchStatus.OK

    def test_http_error_status_is_reported_as_such(self) -> None:
        result = _map(success=False, status_code=404, error_message="Not Found")
        assert result.status is FetchStatus.HTTP_ERROR
        assert result.http_status == 404

    def test_antibot_verdict_on_a_clean_response_stays_a_page(self) -> None:
        """crawl4ai calls small pages "blocked"; the other backends do not.

        Its heuristic flags any page under 5KB with under 50 visible characters,
        which thin-but-real pages trip. Honouring that would mean the same URL is
        an error on one backend and a page on the others.
        """
        result = _map(
            success=False,
            status_code=200,
            html="<html><body><h1>hi</h1></body></html>",
            error_message="Blocked by anti-bot protection: Structural: minimal_text",
        )
        assert result.status is FetchStatus.OK
        assert result.is_success
        # Not swallowed: the reason still reaches the output.
        assert result.error is not None
        assert "anti-bot" in result.error

    def test_a_real_failure_is_still_a_failure(self) -> None:
        """No response and no HTML is a genuine fetch failure, not a thin page."""
        result = _map(success=False, status_code=None, html="", error_message="net::ERR_FAILED")
        assert result.status is FetchStatus.NETWORK_ERROR
        assert not result.is_success

    def test_timeout_is_classified_and_retryable(self) -> None:
        result = _map(
            success=False, status_code=None, html="", error_message="Page.goto: Timeout 30000ms"
        )
        assert result.status is FetchStatus.TIMEOUT
        assert result.status.is_retryable

    def test_an_empty_body_on_a_200_is_not_treated_as_a_page(self) -> None:
        """Nothing to hand back means nothing was really fetched."""
        result = _map(success=False, status_code=200, html="", error_message="empty response")
        assert not result.is_success
