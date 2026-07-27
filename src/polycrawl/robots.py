"""robots.txt fetching and caching.

One robots.txt per host, fetched at most once, with concurrent requests for the
same host collapsing onto a single fetch (otherwise the first burst of N URLs
to a new host triggers N identical downloads).

Fetch failures fail *open* -- a host whose robots.txt 500s or times out is
treated as unrestricted, matching what the major crawlers do. A 401/403 on
robots.txt is the one case treated as "deny all", per the RFC 9309 guidance.
"""

from __future__ import annotations

import asyncio
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from protego import Protego

__all__ = ["RobotsCache", "RobotsRules"]

_MAX_ROBOTS_BYTES = 512 * 1024


@dataclass(slots=True)
class RobotsRules:
    """Parsed rules for one host."""

    parser: Protego | None
    crawl_delay: float | None
    fetched_at: float
    allow_all: bool = False
    deny_all: bool = False

    def can_fetch(self, url: str, agent: str) -> bool:
        if self.deny_all:
            return False
        if self.allow_all or self.parser is None:
            return True
        try:
            return bool(self.parser.can_fetch(url, agent))
        except Exception:
            return True


class RobotsCache:
    """Async, deduplicating robots.txt cache.

    ``urllib`` runs in a worker thread rather than pulling in an async HTTP
    client: robots fetches happen once per host, so the thread cost is
    negligible and the core package stays free of a transitive HTTP dependency
    that would otherwise have to agree with whatever the backends install.
    """

    __slots__ = ("_agent", "_locks", "_rules", "_timeout", "_ttl", "fetch_errors", "hits", "misses")

    def __init__(
        self, user_agent: str = "polycrawl", timeout: float = 10.0, ttl: float = 3600.0
    ) -> None:
        self._rules: dict[str, RobotsRules] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._agent = user_agent
        self._timeout = timeout
        self._ttl = ttl
        self.hits = 0
        self.misses = 0
        self.fetch_errors = 0

    async def get(self, url: str) -> RobotsRules:
        parts = urlsplit(url)
        origin = (parts.scheme, parts.netloc)
        key = f"{parts.scheme}://{parts.netloc}"

        cached = self._rules.get(key)
        if cached is not None and (time.monotonic() - cached.fetched_at) < self._ttl:
            self.hits += 1
            return cached

        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()

        async with lock:
            # A concurrent waiter may have populated the entry meanwhile.
            cached = self._rules.get(key)
            if cached is not None and (time.monotonic() - cached.fetched_at) < self._ttl:
                self.hits += 1
                return cached

            self.misses += 1
            robots_url = urlunsplit((*origin, "/robots.txt", "", ""))
            rules = await asyncio.to_thread(self._fetch, robots_url)
            self._rules[key] = rules
            return rules

    def _fetch(self, robots_url: str) -> RobotsRules:
        now = time.monotonic()
        req = urllib.request.Request(
            robots_url,
            headers={"User-Agent": self._agent, "Accept": "text/plain,*/*"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                if resp.status >= 400:
                    return RobotsRules(None, None, now, allow_all=True)
                body = resp.read(_MAX_ROBOTS_BYTES).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            # 4xx (except auth) means "no rules published" -> everything allowed.
            if exc.code in (401, 403):
                return RobotsRules(None, None, now, deny_all=True)
            return RobotsRules(None, None, now, allow_all=True)
        except Exception:
            self.fetch_errors += 1
            return RobotsRules(None, None, now, allow_all=True)

        if not body.strip():
            return RobotsRules(None, None, now, allow_all=True)

        try:
            parser = Protego.parse(body)
            delay = parser.crawl_delay(self._agent)
            return RobotsRules(parser, float(delay) if delay else None, now)
        except Exception:
            return RobotsRules(None, None, now, allow_all=True)

    async def can_fetch(self, url: str) -> tuple[bool, float | None]:
        """Returns ``(allowed, crawl_delay_seconds)``."""
        rules = await self.get(url)
        return rules.can_fetch(url, self._agent), rules.crawl_delay

    def stats(self) -> dict[str, int]:
        return {
            "robots_cached": len(self._rules),
            "robots_hits": self.hits,
            "robots_misses": self.misses,
            "robots_errors": self.fetch_errors,
        }
