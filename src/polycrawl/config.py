"""Crawl configuration.

One config object drives the engine and every backend, so a crawl is fully
described by a single YAML file or an equivalent set of CLI flags. Every field
can also be set through the environment with a ``POLYCRAWL_`` prefix.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["BrowserSettings", "CrawlConfig", "OutputSettings", "PolitenessSettings"]

ScopeMode = Literal["site", "host", "path", "any"]
WaitUntil = Literal["domcontentloaded", "load", "networkidle", "commit"]


class BrowserSettings(BaseModel):
    """Browser knobs shared by every JS-capable backend."""

    headless: bool = True
    browser_type: Literal["chromium", "firefox", "webkit"] = "chromium"
    #: ``load`` is the default because ``domcontentloaded`` fires before any XHR
    #: has returned, so on a client-rendered page it yields the shell -- nav and
    #: footer, no content -- as a fast HTTP 200 that looks like a success and is
    #: not detectable from the metrics. Measured on a live-scores SPA:
    #: ``domcontentloaded`` 1.21s and 0 of the scores, ``load`` 2.74s and all of
    #: them, ``networkidle`` 3.98s and all of them. ``load`` waits for images and
    #: fonts, which is cheap here because :attr:`block_resources` stops them
    #: being requested at all. Set ``domcontentloaded`` when crawling
    #: server-rendered pages, where it is both faster and sufficient.
    wait_until: WaitUntil = "load"
    #: Hard cap per navigation, milliseconds.
    page_timeout_ms: int = Field(30_000, ge=1_000)
    #: Extra settle time after the wait condition, for frameworks that hydrate
    #: a tick later. Milliseconds; 0 disables.
    settle_ms: int = Field(0, ge=0)
    #: CSS selector to wait for before considering the page ready.
    wait_for_selector: str | None = None
    viewport_width: int = 1280
    viewport_height: int = 800
    user_agent: str | None = None
    #: Block images/media/fonts at the network layer. The single largest
    #: throughput win for text crawling -- typically 60-80% less bytes moved.
    block_resources: bool = True
    blocked_resource_types: list[str] = Field(default_factory=lambda: ["image", "media", "font"])
    #: Drop speculative navigations -- ``<link rel="prefetch">``, Next.js/Nuxt
    #: route prefetching -- which the browser issues for pages we will crawl and
    #: dedup ourselves. Measured on a fixture: 1 prefetch link per page nearly
    #: doubles the HTML a site serves us (56 -> 105 requests over 18 pages at 20
    #: links per page), for **no** change in our own wall time (2.92s -> 2.99s),
    #: because prefetch is issued at lowest priority after the page has already
    #: resolved. So this is a courtesy to the origin, not a speed feature, and it
    #: is off by default because it is not free everywhere -- see
    #: :attr:`blocked_hosts` for why.
    block_prefetch: bool = False
    #: Hosts whose requests are aborted, matched on the request URL's host and
    #: any parent domain, e.g. ``google-analytics.com`` also blocks
    #: ``www.google-analytics.com``. Intended for analytics and tag managers,
    #: which are uncacheable and contribute nothing to extracted content.
    #:
    #: Blocking by *host* rather than by method is deliberate: POST is not a safe
    #: signal, because GraphQL content APIs are POSTs and dropping them silently
    #: empties the page.
    #:
    #: Cost, and why both this and :attr:`block_prefetch` default to off:
    #: aborting anything requires a Playwright route, and registering a route --
    #: even one whose pattern matches nothing -- disables Chromium's HTTP cache
    #: for the whole browser context. Shared bundles are then refetched for every
    #: page: measured 1.63x slower end to end. On the scrapy backend this costs
    #: nothing, because scrapy-playwright installs a route unconditionally and
    #: its cache is already forfeit; on crawlee you are trading 1.63x of your own
    #: throughput for the origin's bandwidth. Enable deliberately.
    blocked_hosts: list[str] = Field(default_factory=list)
    #: Record what the browser fetched per page -- request counts and bytes by
    #: resource type -- and log a summary at the end of the crawl. Uses passive
    #: event listeners, which were measured not to disturb the HTTP cache, so
    #: unlike the blocking options above this is free to leave on.
    trace_resources: bool = False
    #: Recycle a browser page after this many navigations to bound the leaks
    #: that accumulate in long-lived renderer processes.
    max_pages_before_recycle: int = Field(200, ge=0)
    ignore_https_errors: bool = True
    stealth: bool = False
    extra_args: list[str] = Field(default_factory=list)


class PolitenessSettings(BaseModel):
    """Per-host pacing and robots handling."""

    respect_robots: bool = True
    robots_timeout_s: float = 10.0
    #: Starting requests/second per host; adapts up and down from here.
    per_host_rps: float = Field(4.0, gt=0)
    per_host_burst: float = Field(8.0, gt=0)
    min_host_rps: float = Field(0.2, gt=0)
    max_host_rps: float = Field(32.0, gt=0)
    #: Simultaneous in-flight requests to a single host.
    per_host_concurrency: int = Field(4, ge=1)
    #: Let a 429/503 shrink that host's rate, and clean responses grow it.
    adaptive: bool = True
    user_agent_token: str = "polycrawl"

    @model_validator(mode="after")
    def _check_rates(self) -> PolitenessSettings:
        if self.min_host_rps > self.max_host_rps:
            raise ValueError("min_host_rps must be <= max_host_rps")
        return self


class OutputSettings(BaseModel):
    """Where results go and how much of each page is kept."""

    path: Path | None = None
    format: Literal["jsonl", "jsonl.gz", "none"] = "jsonl"
    include_html: bool = False
    include_text: bool = True
    include_links: bool = True
    #: Where a page's ``text`` comes from. ``engine`` extracts it centrally, so
    #: identical HTML yields an identical field on every backend -- the property
    #: that makes backends interchangeable. ``backend`` prefers a backend's own
    #: rendering when it has one (crawl4ai's markdown, say), which is richer but
    #: no longer comparable across backends.
    text_source: Literal["engine", "backend"] = "engine"
    #: Truncate extracted text past this many characters. 0 = unlimited.
    max_text_chars: int = Field(120_000, ge=0)
    #: Flush the write buffer once it exceeds this many bytes.
    buffer_bytes: int = Field(1 << 20, ge=4096)


class CrawlConfig(BaseSettings):
    """Everything needed to run a crawl."""

    model_config = SettingsConfigDict(
        env_prefix="POLYCRAWL_",
        env_nested_delimiter="__",
        extra="forbid",
    )

    seeds: list[str] = Field(default_factory=list)
    backend: str = "crawl4ai"

    # -- scale ------------------------------------------------------------
    #: Global in-flight ceiling. The backend never sees more than this at once.
    concurrency: int = Field(32, ge=1)
    #: URLs handed to the backend per submit. Larger batches amortise the
    #: backend's own scheduling; smaller ones react faster to rate limits.
    batch_size: int = Field(64, ge=1)
    #: Concurrent batches in flight. >1 keeps the browser pool busy while a
    #: previous batch drains its slowest page.
    pipeline_depth: int = Field(2, ge=1)
    #: Coroutines parsing HTML and writing output, independent of fetching.
    processor_workers: int = Field(4, ge=1)

    # -- limits -----------------------------------------------------------
    max_pages: int = Field(1000, ge=1)
    max_depth: int = Field(2, ge=0)
    max_retries: int = Field(2, ge=0)
    retry_backoff_s: float = Field(1.5, ge=0)
    #: Whole-crawl wall-clock budget in seconds. 0 = unlimited.
    time_budget_s: float = Field(0, ge=0)
    #: Bound on queued-but-unfetched URLs, so a link farm cannot exhaust RAM.
    max_frontier: int = Field(500_000, ge=1)
    max_frontier_per_host: int = Field(50_000, ge=1)
    #: Pause submitting new work above this process RSS percentage of system
    #: memory. Browsers are memory-hungry and the OOM killer is not selective.
    memory_limit_pct: float = Field(85.0, gt=0, le=100)

    # -- scope ------------------------------------------------------------
    scope: ScopeMode = "site"
    allow_patterns: list[str] = Field(default_factory=list)
    deny_patterns: list[str] = Field(default_factory=list)
    allow_domains: list[str] = Field(default_factory=list)
    deny_domains: list[str] = Field(default_factory=list)
    follow_links: bool = True
    strip_query: bool = False
    skip_binary: bool = True

    # -- dedup ------------------------------------------------------------
    dedup: Literal["exact", "bloom"] = "exact"
    dedup_capacity: int = Field(10_000_000, ge=1000)
    dedup_error_rate: float = Field(0.001, gt=0, lt=1)

    # -- nested groups ----------------------------------------------------
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    politeness: PolitenessSettings = Field(default_factory=PolitenessSettings)
    output: OutputSettings = Field(default_factory=OutputSettings)

    # -- misc -------------------------------------------------------------
    proxy: str | None = None
    #: Free-form options forwarded verbatim to the selected backend.
    backend_options: dict[str, Any] = Field(default_factory=dict)
    verbose: bool = False
    progress: bool = True

    @field_validator("seeds", mode="before")
    @classmethod
    def _split_seeds(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [s.strip() for s in v.replace(",", "\n").splitlines() if s.strip()]
        return v

    @field_validator("seeds", mode="after")
    @classmethod
    def _add_scheme(cls, v: list[str]) -> list[str]:
        return [u if "://" in u else f"https://{u}" for u in v]

    @model_validator(mode="after")
    def _coherence(self) -> CrawlConfig:
        # Handing the backend more work than the global ceiling would let it
        # exceed that ceiling on its own.
        if self.batch_size > self.concurrency:
            object.__setattr__(self, "batch_size", self.concurrency)
        if self.politeness.per_host_concurrency > self.concurrency:
            self.politeness.per_host_concurrency = self.concurrency
        return self

    @classmethod
    def from_file(cls, path: str | Path, **overrides: Any) -> CrawlConfig:
        """Load YAML (or JSON -- YAML is a superset) and apply overrides."""
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{path}: top level must be a mapping")
        data.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**data)

    def to_yaml(self) -> str:
        return yaml.safe_dump(
            self.model_dump(mode="json", exclude_none=True), sort_keys=False, width=100
        )
