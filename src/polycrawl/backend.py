"""The backend contract.

A *backend* is only a fetcher. It turns :class:`FetchRequest` objects into
:class:`FetchResult` objects and owns whatever machinery that takes -- a
browser pool, an HTTP client, a remote rendering service. Everything else
(frontier ordering, dedup, robots, politeness, retries, link extraction,
output) lives in the engine, so swapping backends changes *how pages are
fetched* and nothing about how the crawl behaves.

The interface is submit/emit rather than a request-response call because every
serious fetching library batches and schedules internally; forcing them through
a one-URL-at-a-time API would throw away their dispatchers and browser reuse.
Backends pull work in whatever grouping suits them and push results back as
soon as each one is ready.

Implementing a new backend
--------------------------
Subclass :class:`CrawlerBackend`, implement the four lifecycle methods, and
advertise it either in ``pyproject.toml``::

    [project.entry-points."polycrawl.backends"]
    my_backend = "my_pkg.backend:MyBackend"

or at runtime with :func:`polycrawl.registry.register`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from .models import FetchRequest, FetchResult

if TYPE_CHECKING:
    from .config import CrawlConfig

__all__ = ["BackendCapabilities", "CrawlerBackend", "EmitFn"]

#: Called by a backend exactly once per submitted request, in completion order.
EmitFn = Callable[[FetchResult], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class BackendCapabilities:
    """What a backend can do, so the engine and CLI can adapt or warn.

    The engine reads these to decide whether it must do work itself -- for
    example, when ``native_links`` is ``False`` it parses links out of the HTML
    with its own extractor.
    """

    #: Executes JavaScript (a browser), as opposed to raw HTTP.
    javascript: bool = False
    #: Returns links it found itself, saving the engine a parse.
    native_links: bool = False
    #: Produces markdown/clean text natively.
    native_markdown: bool = False
    #: Honours ``CrawlConfig.proxy``.
    proxy: bool = False
    #: Offers anti-bot fingerprint evasion.
    stealth: bool = False
    #: Can capture screenshots (not wired into the default pipeline).
    screenshots: bool = False

    def describe(self) -> str:
        flags = [
            ("js", self.javascript),
            ("links", self.native_links),
            ("markdown", self.native_markdown),
            ("proxy", self.proxy),
            ("stealth", self.stealth),
        ]
        return ", ".join(n for n, on in flags if on) or "-"


class CrawlerBackend(ABC):
    """Base class for fetch backends.

    Lifecycle, in order: :meth:`start` -> many :meth:`submit` ->
    :meth:`drain` -> :meth:`close`. ``submit`` must not block waiting for the
    fetches to finish; it hands work off and returns.
    """

    #: Registry key, also what ``--backend`` accepts.
    name: ClassVar[str] = ""
    capabilities: ClassVar[BackendCapabilities] = BackendCapabilities()
    #: Pip extra that provides this backend's dependency, for error messages.
    install_extra: ClassVar[str] = ""

    def __init__(self, config: CrawlConfig) -> None:
        self.config = config
        self._emit: EmitFn | None = None

    # -- availability -----------------------------------------------------

    @classmethod
    def is_available(cls) -> tuple[bool, str]:
        """Whether this backend's dependencies are importable.

        Returns ``(available, reason)``; ``reason`` is shown to the user when
        unavailable. Overridden by backends with optional dependencies.
        """
        return True, ""

    @classmethod
    def require(cls) -> None:
        ok, reason = cls.is_available()
        if not ok:
            hint = (
                f"  Install with:  uv sync --extra {cls.install_extra}" if cls.install_extra else ""
            )
            raise RuntimeError(f"backend {cls.name!r} is unavailable: {reason}\n{hint}")

    # -- lifecycle --------------------------------------------------------

    @abstractmethod
    async def start(self, emit: EmitFn) -> None:
        """Bring up the fetcher and record the callback used to return results.

        Anything expensive and reusable -- browser launch, connection pools --
        belongs here, not in :meth:`submit`.
        """

    @abstractmethod
    async def submit(self, batch: Sequence[FetchRequest]) -> None:
        """Accept work. Returns as soon as the batch is queued, not fetched."""

    @abstractmethod
    async def drain(self) -> None:
        """Block until every submitted request has been emitted."""

    @abstractmethod
    async def close(self) -> None:
        """Release all resources. Must be safe to call after a failed start."""

    # -- helpers for subclasses -------------------------------------------

    async def emit(self, result: FetchResult) -> None:
        """Hand one finished result back to the engine."""
        if self._emit is None:
            raise RuntimeError(f"{type(self).__name__}.start() was not called")
        result.backend = result.backend or self.name
        await self._emit(result)

    @property
    def max_concurrency(self) -> int:
        return self.config.concurrency

    async def __aenter__(self) -> CrawlerBackend:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"
