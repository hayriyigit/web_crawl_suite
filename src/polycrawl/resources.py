"""Deciding what a browser is allowed to fetch, and recording what it did.

Three mechanisms are available to a Playwright-based backend, and they are not
interchangeable -- each was measured against a fixture that counts requests
server-side (see ``docs/engineering-notes.md``):

=========================== ================= ==========================
mechanism                   cost              can express
=========================== ================= ==========================
launch flags                free              images, fonts only
``context.route(...)``      **1.63x slower**  anything
``page.on("request")``      free              nothing -- observation only
=========================== ================= ==========================

The middle row is the surprising one. Registering *any* route disables
Chromium's HTTP cache for the whole browser context, so shared bundles are
refetched on every page. It is not about what the handler does: ``continue_()``
and ``fallback()`` behave identically, and a pattern matching no URL at all is
just as destructive. That is why blocking beyond images and fonts is opt-in, and
why this module keeps the decision in one place -- a caller that enables nothing
must not end up with a route installed.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .config import BrowserSettings

__all__ = ["BlockPolicy", "ResourceTrace", "attach_trace", "is_prefetch"]

#: Headers Chromium sets on speculative navigations. ``Purpose: prefetch`` is
#: what ``<link rel="prefetch">`` sends today; ``Sec-Purpose`` is the newer
#: spelling used by the Speculation Rules API. Either means "not needed now",
#: which is exactly the request we want to drop.
_PREFETCH_HEADERS = ("purpose", "sec-purpose")


def is_prefetch(headers: dict[str, str]) -> bool:
    """Whether a request is a speculative navigation, by its own declaration.

    Header names are matched case-insensitively; Playwright lowercases them in
    ``all_headers()`` but a caller may pass raw ones.
    """
    for name in _PREFETCH_HEADERS:
        value = headers.get(name) or headers.get(name.title()) or headers.get(name.upper())
        if value and "prefetch" in value.lower():
            return True
    return False


def _host_of(url: str) -> str:
    """The host part of a URL, lowercased and without port or credentials."""
    rest = url.split("://", 1)[-1]
    authority = rest.split("/", 1)[0]
    if "@" in authority:
        authority = authority.rsplit("@", 1)[1]
    if authority.startswith("["):  # IPv6 literal
        return authority.partition("]")[0].lstrip("[").lower()
    return authority.split(":", 1)[0].lower()


@dataclass(frozen=True)
class BlockPolicy:
    """What to abort, and whether aborting is needed at all.

    :attr:`needed` is the important part: when it is false a backend must not
    install a route, because doing so would cost the HTTP cache for nothing.
    """

    prefetch: bool = False
    hosts: frozenset[str] = frozenset()
    #: Resource types the launch flags could not express (e.g. ``stylesheet``).
    resource_types: frozenset[str] = frozenset()

    @classmethod
    def from_settings(
        cls, browser: BrowserSettings, *, leftover_types: frozenset[str] = frozenset()
    ) -> BlockPolicy:
        return cls(
            prefetch=browser.block_prefetch,
            hosts=frozenset(
                h.strip().lower().lstrip(".") for h in browser.blocked_hosts if h.strip()
            ),
            resource_types=leftover_types,
        )

    @property
    def needed(self) -> bool:
        return bool(self.prefetch or self.hosts or self.resource_types)

    def blocks_host(self, url: str) -> bool:
        """Whether *url*'s host, or any parent domain of it, is on the denylist."""
        if not self.hosts:
            return False
        host = _host_of(url)
        if host in self.hosts:
            return True
        # `google-analytics.com` should also match `www.google-analytics.com`,
        # but must not match `notgoogle-analytics.com`.
        return any(host.endswith(f".{blocked}") for blocked in self.hosts)

    def block_reason(self, url: str, resource_type: str, headers: dict[str, str]) -> str | None:
        """Why this request should be dropped, or ``None`` to allow it.

        The reason is what the resource trace counts, so it has to name the rule
        that fired rather than just report that one did.
        """
        if self.prefetch and is_prefetch(headers):
            return "prefetch"
        if self.resource_types and resource_type in self.resource_types:
            return resource_type
        if self.blocks_host(url):
            return "host"
        return None

    def should_block(self, url: str, resource_type: str, headers: dict[str, str]) -> bool:
        return self.block_reason(url, resource_type, headers) is not None


@dataclass
class ResourceTrace:
    """Counts of what the browser fetched, by resource type.

    Fed from passive event listeners, which were measured not to perturb the
    cache, so the numbers describe an ordinary crawl rather than a traced one.

    :attr:`requests` counts what the *page asked for*, which includes requests
    the browser answered from its own cache without touching the network. That
    distinction is the whole point of :attr:`from_cache`: a shared bundle
    refetched on every page rather than served from cache is the single most
    expensive mistake available here (1.63x end to end), and it is invisible if
    you only count requests.
    """

    requests: Counter[str] = field(default_factory=Counter)
    bytes_by_type: Counter[str] = field(default_factory=Counter)
    blocked: Counter[str] = field(default_factory=Counter)
    #: Populated only on Chromium, where CDP reports cache disposition exactly.
    from_cache: int = 0
    from_network: int = 0
    pages: int = 0

    def record_request(self, resource_type: str) -> None:
        self.requests[resource_type or "other"] += 1

    def record_bytes(self, resource_type: str, size: int) -> None:
        if size > 0:
            self.bytes_by_type[resource_type or "other"] += size

    def record_blocked(self, reason: str) -> None:
        self.blocked[reason] += 1

    def record_page(self) -> None:
        self.pages += 1

    def record_cache(self, *, hit: bool) -> None:
        if hit:
            self.from_cache += 1
        else:
            self.from_network += 1

    @property
    def total_requests(self) -> int:
        return sum(self.requests.values())

    @property
    def total_bytes(self) -> int:
        return sum(self.bytes_by_type.values())

    @property
    def cache_hit_rate(self) -> float | None:
        """Share of resolved requests served from cache, or ``None`` if unknown."""
        seen = self.from_cache + self.from_network
        return self.from_cache / seen if seen else None

    def summary(self) -> str:
        """A short table, ordered by request count."""
        if not self.requests:
            return "resource trace: nothing observed"
        pages = max(self.pages, 1)
        lines = [
            f"resource trace: {self.total_requests} requests, "
            f"{self.total_bytes / 1e6:.2f} MB over {self.pages} page(s) "
            f"({self.total_requests / pages:.1f} requests/page)"
        ]
        for rtype, count in self.requests.most_common():
            size = self.bytes_by_type.get(rtype, 0)
            lines.append(
                f"  {rtype:<14} {count:>6} req  {count / pages:>6.1f}/page  {size / 1e6:>7.2f} MB"
            )
        for reason, count in self.blocked.most_common():
            lines.append(f"  blocked:{reason:<6} {count:>6} req  {count / pages:>6.1f}/page")
        rate = self.cache_hit_rate
        if rate is not None:
            lines.append(
                f"  cache          {self.from_cache} hit / {self.from_network} network "
                f"({rate:.0%} served from cache)"
            )
        return "\n".join(lines)


async def attach_trace(page: Any, trace: ResourceTrace) -> None:
    """Wire *page* up to *trace*, without changing how the page behaves.

    Request and response events are plain listeners. Cache disposition needs CDP,
    which is Chromium-only and was verified not to disable the HTTP cache the way
    a route does -- but it is still best effort: on another browser, or if the
    session cannot be opened, the trace simply carries no cache figures rather
    than failing the crawl.
    """
    page.on("request", lambda request: trace.record_request(request.resource_type))

    def _on_response(response: Any) -> None:
        try:
            size = int(response.headers.get("content-length") or 0)
        except (TypeError, ValueError):
            size = 0
        trace.record_bytes(response.request.resource_type, size)

    page.on("response", _on_response)

    try:
        session = await page.context.new_cdp_session(page)
        await session.send("Network.enable")
    except Exception:  # pragma: no cover - non-Chromium, or a page already gone
        return

    # Chromium reports the two cache tiers through different events, and a
    # request can appear in both, so the disposition is resolved once per
    # requestId at responseReceived rather than counted at each event.
    from_memory_cache: set[str] = set()

    def _on_served_from_cache(event: dict[str, Any]) -> None:
        request_id = event.get("requestId")
        if request_id:
            from_memory_cache.add(request_id)

    def _on_cdp_response(event: dict[str, Any]) -> None:
        response = event.get("response") or {}
        request_id = event.get("requestId")
        cached = bool(response.get("fromDiskCache") or response.get("fromPrefetchCache"))
        if request_id is not None and request_id in from_memory_cache:
            cached = True
            from_memory_cache.discard(request_id)
        trace.record_cache(hit=cached)

    session.on("Network.requestServedFromCache", _on_served_from_cache)
    session.on("Network.responseReceived", _on_cdp_response)
    # The session must outlive this function. Nothing else holds a reference to
    # it, and a collected session takes its listeners with it -- which shows up
    # as a trace that reports every request as a network fetch and no cache hits
    # at all, rather than as an error.
    page._polycrawl_cdp_session = session
