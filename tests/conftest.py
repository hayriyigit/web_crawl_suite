from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, ClassVar

import pytest

from polycrawl.backend import BackendCapabilities, CrawlerBackend, EmitFn
from polycrawl.models import FetchRequest, FetchResult, FetchStatus


class FakeBackend(CrawlerBackend):
    """In-memory backend used to test the engine without launching a browser.

    Doubles as the reference example of the backend contract: four lifecycle
    methods, results handed back through :meth:`emit`.
    """

    name: ClassVar[str] = "fake"
    capabilities: ClassVar[BackendCapabilities] = BackendCapabilities(javascript=False)

    #: url -> html, or a callable returning a FetchResult for error injection.
    pages: ClassVar[dict[str, Any]] = {}

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        self.tasks: set[asyncio.Task[None]] = set()
        self.seen_urls: list[str] = []
        self.max_observed_in_flight = 0
        self._in_flight = 0
        self.started = False
        self.closed = False

    async def start(self, emit: EmitFn) -> None:
        self._emit = emit
        self.started = True

    async def submit(self, batch: Sequence[FetchRequest]) -> None:
        task = asyncio.create_task(self._run(list(batch)))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _run(self, batch: list[FetchRequest]) -> None:
        self._in_flight += len(batch)
        self.max_observed_in_flight = max(self.max_observed_in_flight, self._in_flight)
        await asyncio.sleep(0)
        for req in batch:
            self.seen_urls.append(req.url)
            await self.emit(self._make(req))
            self._in_flight -= 1

    def _make(self, req: FetchRequest) -> FetchResult:
        entry = self.pages.get(req.url)
        if callable(entry):
            return entry(req)
        if entry is None:
            return FetchResult(
                request=req, status=FetchStatus.HTTP_ERROR, http_status=404, backend=self.name
            )
        return FetchResult(
            request=req,
            status=FetchStatus.OK,
            url=req.url,
            http_status=200,
            html=entry,
            elapsed=0.01,
            backend=self.name,
        )

    async def drain(self) -> None:
        while True:
            pending = [t for t in self.tasks if not t.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    async def close(self) -> None:
        self.closed = True
        await self.drain()


@pytest.fixture
def fake_backend_cls() -> type[FakeBackend]:
    FakeBackend.pages = {}
    return FakeBackend
