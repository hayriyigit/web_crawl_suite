"""Backend discovery and the third-party extension path."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import ClassVar

import pytest

from polycrawl import CrawlConfig, CrawlEngine, MemorySink
from polycrawl.backend import BackendCapabilities, CrawlerBackend, EmitFn
from polycrawl.models import FetchRequest, FetchResult, FetchStatus
from polycrawl.registry import get_backend, list_backends, register


class TestDiscovery:
    def test_builtins_are_discovered(self) -> None:
        names = {b.name for b in list_backends()}
        assert {"crawl4ai", "crawlee", "scrapy"} <= names

    def test_get_backend_returns_the_class(self) -> None:
        cls = get_backend("crawl4ai")
        assert cls.name == "crawl4ai"
        assert issubclass(cls, CrawlerBackend)

    def test_unknown_backend_names_the_alternatives(self) -> None:
        with pytest.raises(KeyError, match="Available"):
            get_backend("does-not-exist")

    def test_capabilities_are_reported(self) -> None:
        info = {b.name: b for b in list_backends()}
        assert "js" in info["crawl4ai"].capabilities
        assert "js" in info["crawlee"].capabilities
        assert "js" in info["scrapy"].capabilities

    def test_importing_the_registry_does_not_import_backends(self) -> None:
        """Discovery must stay cheap even with heavy optional dependencies."""
        import subprocess
        import sys

        code = (
            "import sys; import polycrawl.registry as r; r.list_backends;"
            "print('crawl4ai' in sys.modules, 'playwright' in sys.modules,"
            " 'scrapy' in sys.modules, 'twisted' in sys.modules)"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        assert out.stdout.strip() == "False False False False"

    def test_importing_the_scrapy_backend_installs_no_reactor(self) -> None:
        """Importing a module must not mutate process-global Twisted state.

        The reactor is installed once per process and cannot be replaced, so a
        backend that installed one at import time would decide it for the whole
        host application.
        """
        import subprocess
        import sys

        code = (
            "import polycrawl.backends.scrapy_backend as b;"
            "from scrapy.utils.reactor import is_reactor_installed;"
            "print(is_reactor_installed(), b.ScrapyBackend.name)"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        assert out.stdout.strip() == "False scrapy"


class TestThirdPartyBackend:
    """A backend defined entirely outside the package must work unchanged."""

    async def test_register_and_run_a_custom_backend(self) -> None:
        calls: list[str] = []

        @register
        class StaticBackend(CrawlerBackend):
            name: ClassVar[str] = "static-test"
            capabilities: ClassVar[BackendCapabilities] = BackendCapabilities(javascript=False)

            async def start(self, emit: EmitFn) -> None:
                self._emit = emit

            async def submit(self, batch: Sequence[FetchRequest]) -> None:
                for req in batch:
                    calls.append(req.url)
                    await self.emit(
                        FetchResult(
                            request=req,
                            status=FetchStatus.OK,
                            url=req.url,
                            http_status=200,
                            html="<html><title>S</title><body>hi</body></html>",
                            backend=self.name,
                        )
                    )

            async def drain(self) -> None:
                return None

            async def close(self) -> None:
                return None

        assert get_backend("static-test") is StaticBackend
        assert "static-test" in {b.name for b in list_backends()}

        cfg = CrawlConfig(
            seeds=["https://custom.test/"],
            backend="static-test",
            max_pages=1,
            progress=False,
            output={"format": "none"},
            politeness={"respect_robots": False},
        )
        sink = MemorySink()
        # Resolved by name through the registry, not passed in directly.
        metrics = await asyncio.wait_for(CrawlEngine(cfg, sink=sink).run(), timeout=15)

        assert calls == ["https://custom.test/"]
        assert metrics.succeeded == 1
        assert sink.pages[0].title == "S"
        assert sink.pages[0].backend == "static-test"

    async def test_unavailable_backend_explains_how_to_install(self) -> None:
        class MissingDep(CrawlerBackend):
            name: ClassVar[str] = "missing-dep"
            install_extra: ClassVar[str] = "somepkg"

            @classmethod
            def is_available(cls) -> tuple[bool, str]:
                return False, "somepkg is not installed"

            async def start(self, emit: EmitFn) -> None: ...
            async def submit(self, batch: Sequence[FetchRequest]) -> None: ...
            async def drain(self) -> None: ...
            async def close(self) -> None: ...

        with pytest.raises(RuntimeError, match="uv sync --extra somepkg"):
            MissingDep.require()
