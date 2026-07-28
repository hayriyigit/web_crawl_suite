"""Fetch without a browser, and escalate to one only where it is needed.

Rendering everything costs 6-10x the latency and 3-5 GB of RAM per process, and
most pages do not need it -- of four real sites measured, two returned the same
text without a browser. Rendering nothing silently loses content on the two that
do, as an HTTP 200 with no error, which is the worst failure mode available: it
looks like success and nothing in the metrics disagrees.

:class:`RoutingFetchService` does neither. Every URL is fetched without a browser
first; the result is judged; only the ones that come back thin are refetched with
one. The escalation rate is what sizes the browser tier, and it is reported.

    routing = RoutingFetchService.from_config(config)
    await routing.start()                 # no browser launched yet
    pages = await routing.fetch([...])    # browser starts on the first escalation
    print(routing.snapshot()["escalation_rate"])

Both tiers deliberately **share** one rate limiter and one robots cache. Two
:class:`FetchService` instances with their own would let a single host see twice
the configured rate -- the same defect ``docs/deployment.md`` warns about for
multiple processes, reintroduced inside one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from .models import CrawledPage
from .service import FetchService
from .urls import host_of

if TYPE_CHECKING:
    from .config import CrawlConfig

log = logging.getLogger("polycrawl.routing")

__all__ = ["ContentCheck", "RoutingFetchService", "Verdict"]


@dataclass(frozen=True, slots=True)
class Verdict:
    """Whether a browserless result is good enough, and why."""

    sufficient: bool
    reason: str = ""


#: A caller-supplied judgement. Returning a bare bool is accepted; the reason is
#: only used for logging and the escalation breakdown.
SufficientFn = Callable[[CrawledPage], "Verdict | bool"]


@dataclass(frozen=True, slots=True)
class ContentCheck:
    """The default judgement: is this page plausibly complete?

    The thresholds are fitted to four real sites, and no single signal separates
    them -- which is why there are two, combined with OR:

    ==========  ============  ======  =========  ==========
    site        needs render?   text  text/html  rendered
    ==========  ============  ======  =========  ==========
    tcmb.gov.tr yes             1949      0.062  2.32x more
    mackolik    yes             5222      0.017  3.70x more
    havelsan    no              4613      0.054  1.00x
    doviz.com   no             18188      0.057  1.01x
    ==========  ============  ======  =========  ==========

    A text floor alone catches tcmb and misses mackolik. A text-to-HTML ratio
    alone catches mackolik and misses tcmb -- whose ratio is *higher* than either
    site that needs no browser. Together they classify all four correctly.

    Four sites is a small sample and these numbers are fitted to it, so treat
    them as a starting point rather than a law. ``require_selector`` is the
    reliable signal when you know what you are looking for: if the caller can
    name an element that must be present, no heuristic is needed.
    """

    #: Below this many characters of text, assume the page did not finish.
    min_text: int = 2500
    #: Text characters per byte of HTML. A page that is mostly markup and script
    #: with little prose is the classic un-hydrated SPA shell.
    min_text_ratio: float = 0.03
    #: A string that must appear in the page's **extracted text**. Exact, and
    #: worth using whenever the caller knows what it is looking for.
    #:
    #: Matched against text rather than HTML on purpose. The marker for
    #: content that JavaScript builds is usually present in the raw HTML
    #: already -- inside the unexecuted script that would have built it -- so
    #: an HTML match reports success on exactly the page that needs a browser.
    #: Extracted text only contains what the DOM actually rendered.
    require_marker: str | None = None

    def __call__(self, page: CrawledPage) -> Verdict:
        if page.status != "ok":
            # 403 is often anti-bot, which a real browser frequently passes;
            # a 404 is simply a 404 and rendering it again wastes a browser slot.
            if page.http_status == 403:
                return Verdict(False, "http-403")
            return Verdict(True, "not-ok")

        html = page.html or ""
        text = page.text or ""

        if self.require_marker is not None:
            if self.require_marker in text:
                return Verdict(True, "marker-present")
            return Verdict(False, "marker-missing")

        if len(text) < self.min_text:
            return Verdict(False, f"thin-text:{len(text)}")
        if html and len(text) / len(html) < self.min_text_ratio:
            return Verdict(False, f"low-text-ratio:{len(text) / len(html):.3f}")
        return Verdict(True, "ok")


@dataclass
class RoutingStats:
    """Counters specific to routing; each tier keeps its own :class:`ServiceStats`."""

    requested: int = 0
    served_plain: int = 0
    escalated: int = 0
    escalation_failed: int = 0
    rendered_not_better: int = 0
    host_cache_hits: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def record_reason(self, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        rate = self.escalated / self.requested if self.requested else 0.0
        return {
            "requested": self.requested,
            "served_plain": self.served_plain,
            "escalated": self.escalated,
            "escalation_rate": round(rate, 3),
            "escalation_failed": self.escalation_failed,
            "rendered_not_better": self.rendered_not_better,
            "host_cache_hits": self.host_cache_hits,
            "escalation_reasons": dict(self.reasons),
        }


class RoutingFetchService:
    """Browserless first, browser only where the result comes back thin.

    Args:
        plain: The no-browser tier. Every URL starts here.
        rendered: The browser tier, started lazily on the first escalation so a
            deployment that never needs one never pays for it.
        sufficient: Judgement applied to every plain result. Defaults to
            :class:`ContentCheck`.
        host_ttl_s: How long to remember that a host needed rendering, so later
            URLs from it skip the wasted plain attempt. Expires because sites get
            rebuilt; 0 disables the cache.
    """

    def __init__(
        self,
        plain: FetchService,
        rendered: FetchService,
        *,
        sufficient: SufficientFn | None = None,
        host_ttl_s: float = 3600.0,
        keep_html: bool = True,
    ) -> None:
        self.plain = plain
        self.rendered = rendered
        self.sufficient: SufficientFn = sufficient or ContentCheck()
        self.host_ttl_s = host_ttl_s
        self.stats = RoutingStats()
        #: The plain tier is forced to keep HTML so the judgement can use it;
        #: this records whether the *caller* actually wanted it back.
        self._keep_html = keep_html

        # One authority for pacing and robots across both tiers. Without this a
        # host would be paced twice over, and its robots.txt fetched twice.
        self.rendered.limiter = self.plain.limiter
        self.rendered.robots = self.plain.robots

        #: host -> (needs_render, expires_at)
        self._host_verdicts: dict[str, tuple[bool, float]] = {}
        self._render_lock = asyncio.Lock()
        self._closed = False

    @classmethod
    def from_config(
        cls,
        config: CrawlConfig,
        *,
        rendered_config: CrawlConfig | None = None,
        sufficient: SufficientFn | None = None,
        host_ttl_s: float = 3600.0,
        **service_kwargs: Any,
    ) -> RoutingFetchService:
        """Build both tiers from one config.

        The plain tier is forced to keep HTML, because the judgement needs it and
        ``output.include_html`` is off by default. HTML is stripped again from
        anything returned unless the caller asked for it.
        """
        plain_config = config.model_copy(deep=True)
        plain_config.backend_options = {**plain_config.backend_options, "render": False}
        plain_config.output.include_html = True

        rendered = rendered_config or config.model_copy(deep=True)
        rendered.backend_options = {
            k: v for k, v in rendered.backend_options.items() if k != "render"
        }

        return cls(
            FetchService(plain_config, **service_kwargs),
            FetchService(rendered, **service_kwargs),
            sufficient=sufficient,
            host_ttl_s=host_ttl_s,
            keep_html=config.output.include_html,
        )

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        """Start the browserless tier. The browser waits for a reason to exist."""
        await self.plain.start()

    async def close(self) -> None:
        self._closed = True
        await self.plain.close()
        if self.rendered.ready:
            await self.rendered.close()

    async def __aenter__(self) -> RoutingFetchService:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def ready(self) -> bool:
        return self.plain.ready and not self._closed

    # -- public API -------------------------------------------------------

    async def fetch(self, urls: Sequence[str]) -> list[CrawledPage]:
        if not self.ready:
            raise RuntimeError("RoutingFetchService.start() was not called")
        if not urls:
            return []
        return list(await asyncio.gather(*(self._route(u) for u in urls)))

    async def fetch_one(self, url: str) -> CrawledPage:
        pages = await self.fetch([url])
        return pages[0]

    def snapshot(self) -> dict[str, Any]:
        return {
            "routing": self.stats.as_dict(),
            "hosts_needing_render": sum(1 for needs, _ in self._host_verdicts.values() if needs),
            "browser_started": self.rendered.ready,
            "plain": self.plain.snapshot(),
            "rendered": self.rendered.snapshot() if self.rendered.ready else None,
        }

    # -- routing ----------------------------------------------------------

    async def _route(self, url: str) -> CrawledPage:
        self.stats.requested += 1
        host = host_of(url)

        if self._host_needs_render(host):
            self.stats.host_cache_hits += 1
            self.stats.escalated += 1
            self.stats.record_reason("host-cached")
            page = await self._render(url)
            return self._present(page if page is not None else await self.plain.fetch_one(url))

        page = await self.plain.fetch_one(url)
        verdict = self._judge(page)
        if verdict.sufficient:
            self.stats.served_plain += 1
            return self._present(page)

        self.stats.escalated += 1
        self.stats.record_reason(verdict.reason)
        log.debug("escalating %s to a browser (%s)", url, verdict.reason)

        rendered = await self._render(url)
        if rendered is None:
            self.stats.escalation_failed += 1
            return self._present(page)

        # Keep whichever actually has more content. A browser can fail in ways
        # that still produce a page -- a timeout mid-load, a consent wall -- and
        # returning less than the free attempt already got would be a regression.
        if len(rendered.text or "") < len(page.text or ""):
            self.stats.rendered_not_better += 1
            return self._present(page)

        self._remember(host, needs_render=True)
        return self._present(rendered)

    def _judge(self, page: CrawledPage) -> Verdict:
        result = self.sufficient(page)
        return result if isinstance(result, Verdict) else Verdict(bool(result))

    async def _render(self, url: str) -> CrawledPage | None:
        """Fetch through the browser tier, starting it if this is the first time."""
        try:
            if not self.rendered.ready:
                async with self._render_lock:
                    if not self.rendered.ready:
                        log.info("starting the browser tier (first escalation)")
                        await self.rendered.start()
            return await self.rendered.fetch_one(url)
        except Exception as exc:
            # The browser tier is the optional half: if it cannot start or the
            # fetch fails, the caller still gets the browserless result.
            log.warning("escalation to a browser failed for %s: %s", url, exc)
            return None

    def _present(self, page: CrawledPage) -> CrawledPage:
        """Drop the HTML the judgement needed, unless the caller wants it."""
        if self._keep_html or page.html is None:
            return page
        return replace(page, html=None)

    # -- per-host memory --------------------------------------------------

    def _host_needs_render(self, host: str) -> bool:
        if not host or self.host_ttl_s <= 0:
            return False
        entry = self._host_verdicts.get(host)
        if entry is None:
            return False
        needs, expires_at = entry
        if time.monotonic() >= expires_at:
            del self._host_verdicts[host]
            return False
        return needs

    def _remember(self, host: str, *, needs_render: bool) -> None:
        if host and self.host_ttl_s > 0:
            self._host_verdicts[host] = (needs_render, time.monotonic() + self.host_ttl_s)
