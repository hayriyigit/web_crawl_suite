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
    #: ``domcontentloaded`` is the default deliberately: it fires once the HTML
    #: is parsed and synchronous scripts have run, which is enough for
    #: client-rendered content, while ``load``/``networkidle`` wait on images,
    #: fonts, analytics beacons and long-poll connections that never settle.
    wait_until: WaitUntil = "domcontentloaded"
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
