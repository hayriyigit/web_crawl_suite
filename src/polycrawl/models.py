"""Core data types exchanged between the engine, the backends and the sinks.

These are deliberately plain dataclasses rather than pydantic models: they are
allocated once per fetched URL, so on a multi-million-page crawl the validation
overhead and the per-instance ``__dict__`` would both show up in profiles.
``slots=True`` keeps them compact.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class FetchStatus(StrEnum):
    """Outcome of a single fetch attempt."""

    OK = "ok"
    HTTP_ERROR = "http_error"
    TIMEOUT = "timeout"
    BLOCKED_ROBOTS = "blocked_robots"
    NETWORK_ERROR = "network_error"
    BACKEND_ERROR = "backend_error"
    SKIPPED = "skipped"

    @property
    def is_success(self) -> bool:
        return self is FetchStatus.OK

    @property
    def is_retryable(self) -> bool:
        return self in _RETRYABLE


_RETRYABLE = frozenset({FetchStatus.TIMEOUT, FetchStatus.NETWORK_ERROR, FetchStatus.BACKEND_ERROR})


@dataclass(slots=True)
class FetchRequest:
    """A single unit of work handed to a backend."""

    url: str
    depth: int = 0
    parent: str | None = None
    attempt: int = 0
    #: Opaque per-request hints a backend may honour (e.g. ``{"js_code": "..."}``).
    meta: dict[str, Any] = field(default_factory=dict)
    enqueued_at: float = field(default_factory=time.monotonic)

    @property
    def host(self) -> str:
        from .urls import host_of

        return host_of(self.url)


@dataclass(slots=True)
class FetchResult:
    """What a backend returns for one :class:`FetchRequest`.

    Backends fill in what they can. ``html`` is the rendered DOM (post-JS) when
    the backend is browser-based; link extraction and text conversion are done
    once, centrally, by the engine so every backend behaves identically.
    """

    request: FetchRequest
    status: FetchStatus
    #: Final URL after redirects.
    url: str = ""
    http_status: int | None = None
    html: str = ""
    #: Backend-provided markdown/text, when the backend produces one natively.
    text: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    #: Links the backend already extracted; the engine falls back to its own
    #: parser when this is ``None``.
    links: list[str] | None = None
    error: str | None = None
    elapsed: float = 0.0
    backend: str = ""
    fetched_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.url:
            self.url = self.request.url

    @property
    def is_success(self) -> bool:
        return self.status.is_success


@dataclass(slots=True)
class CrawledPage:
    """A fully processed page, as written to the output sink."""

    url: str
    final_url: str
    depth: int
    parent: str | None
    http_status: int | None
    status: str
    title: str
    text: str
    html: str | None
    links: list[str]
    n_links: int
    error: str | None
    elapsed: float
    backend: str
    fetched_at: float

    def to_dict(self, include_html: bool = False) -> dict[str, Any]:
        d = {
            "url": self.url,
            "final_url": self.final_url,
            "depth": self.depth,
            "parent": self.parent,
            "http_status": self.http_status,
            "status": self.status,
            "title": self.title,
            "text": self.text,
            "links": self.links,
            "n_links": self.n_links,
            "error": self.error,
            "elapsed_s": round(self.elapsed, 4),
            "backend": self.backend,
            "fetched_at": self.fetched_at,
        }
        if include_html:
            d["html"] = self.html
        return d
