"""Output sinks.

Writing is buffered and pushed to disk from a worker thread. Serialising with
orjson and batching syscalls keeps the writer off the critical path -- at a few
hundred pages/second with HTML retained, a naive per-record ``write()`` plus
``json.dumps`` becomes a measurable share of the loop.
"""

from __future__ import annotations

import asyncio
import gzip
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import IO

import orjson

from .models import CrawledPage

__all__ = ["JsonlSink", "MemorySink", "NullSink", "Sink", "make_sink"]


class Sink(ABC):
    """Destination for crawled pages."""

    @abstractmethod
    async def write(self, page: CrawledPage) -> None: ...

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def __aenter__(self) -> Sink:
        await self.open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


class NullSink(Sink):
    """Discards everything; useful for benchmarking the fetch path alone."""

    async def write(self, page: CrawledPage) -> None:
        return None


class MemorySink(Sink):
    """Keeps pages in a list. For tests and small in-process crawls."""

    def __init__(self) -> None:
        self.pages: list[CrawledPage] = []

    async def write(self, page: CrawledPage) -> None:
        self.pages.append(page)


class JsonlSink(Sink):
    """Newline-delimited JSON, optionally gzipped.

    Buffers serialised records in memory and flushes through
    ``asyncio.to_thread`` so blocking disk I/O never stalls the event loop.
    """

    def __init__(
        self,
        path: Path | None,
        *,
        include_html: bool = False,
        gzip_output: bool = False,
        buffer_bytes: int = 1 << 20,
    ) -> None:
        self.path = path
        self.include_html = include_html
        self.gzip_output = gzip_output
        self.buffer_bytes = buffer_bytes
        self._buf: list[bytes] = []
        self._buf_size = 0
        self._fh: IO[bytes] | None = None
        self._lock = asyncio.Lock()
        self.records = 0
        self.bytes_written = 0

    async def open(self) -> None:
        if self.path is None:
            return
        self._fh = await asyncio.to_thread(self._open_file)

    def _open_file(self) -> IO[bytes]:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.gzip_output:
            return gzip.open(self.path, "wb", compresslevel=4)
        # buffering=0 because this class already batches writes; a second layer
        # of buffering would only add a copy.
        return open(self.path, "wb", buffering=0)

    async def write(self, page: CrawledPage) -> None:
        line = orjson.dumps(
            page.to_dict(include_html=self.include_html),
            option=orjson.OPT_APPEND_NEWLINE,
        )
        async with self._lock:
            self._buf.append(line)
            self._buf_size += len(line)
            self.records += 1
            if self._buf_size >= self.buffer_bytes:
                await self._flush_locked()

    async def _flush_locked(self) -> None:
        if not self._buf:
            return
        blob = b"".join(self._buf)
        self._buf.clear()
        self._buf_size = 0
        self.bytes_written += len(blob)
        if self._fh is not None:
            await asyncio.to_thread(self._fh.write, blob)
        else:
            # No path configured -> stream to stdout so the tool composes with
            # shell pipelines.
            await asyncio.to_thread(sys.stdout.buffer.write, blob)

    async def flush(self) -> None:
        async with self._lock:
            await self._flush_locked()

    async def close(self) -> None:
        await self.flush()
        if self._fh is not None:
            fh, self._fh = self._fh, None
            await asyncio.to_thread(fh.close)
        else:
            await asyncio.to_thread(sys.stdout.buffer.flush)


def make_sink(
    path: Path | None,
    fmt: str = "jsonl",
    *,
    include_html: bool = False,
    buffer_bytes: int = 1 << 20,
) -> Sink:
    if fmt == "none":
        return NullSink()
    if fmt in ("jsonl", "jsonl.gz"):
        gz = fmt == "jsonl.gz" or (path is not None and path.suffix == ".gz")
        return JsonlSink(path, include_html=include_html, gzip_output=gz, buffer_bytes=buffer_bytes)
    raise ValueError(f"unknown output format: {fmt!r}")
