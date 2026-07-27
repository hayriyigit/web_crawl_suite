"""polycrawl behind FastAPI.

Deployable two ways, and the difference matters once you put nginx in front --
see docs/deployment.md:

1. As its own service that your API calls over HTTP (recommended). One process
   owns the fetch pool, so per-host pacing is correct no matter how many API
   replicas you run.
2. Mounted into your existing app. Simpler, one less hop, but every worker gets
   its own rate limiter, so N workers can hit one host N times the intended rate.

Run it:

    uv sync --extra scrapy --extra service
    uv run uvicorn examples.fastapi_service:app --port 8080

    curl -X POST localhost:8080/fetch -H 'content-type: application/json' \
         -d '{"urls": ["https://example.com", "https://example.org"]}'

The service is created **once per process** in the lifespan hook, never per
request: a per-request service would build a connection pool -- or launch a
browser -- for every call.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Body, FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

from polycrawl import CrawlConfig, FetchService, ServiceBusy

log = logging.getLogger("crawl-service")

#: Env-tunable so the same image can be sized differently per deployment.
CONCURRENCY = int(os.getenv("CRAWL_CONCURRENCY", "32"))
PER_HOST_RPS = float(os.getenv("CRAWL_PER_HOST_RPS", "4"))
REQUEST_TIMEOUT = float(os.getenv("CRAWL_REQUEST_TIMEOUT", "20"))
QUEUE_TIMEOUT = float(os.getenv("CRAWL_QUEUE_TIMEOUT", "5"))
MAX_URLS_PER_REQUEST = int(os.getenv("CRAWL_MAX_URLS_PER_REQUEST", "20"))


def build_service() -> FetchService:
    config = CrawlConfig(
        # Any seed satisfies the config; the service never uses it.
        seeds=["https://example.com"],
        backend=os.getenv("CRAWL_BACKEND", "scrapy"),
        # No browser: several times the throughput at a fraction of the memory.
        # Switch to {} to render, and read docs/js-rendering.md first.
        backend_options={"render": False},
        concurrency=CONCURRENCY,
        max_pages=1,
        max_retries=1,
        progress=False,
        politeness={
            "respect_robots": True,
            "per_host_rps": PER_HOST_RPS,
            "per_host_concurrency": int(os.getenv("CRAWL_PER_HOST_CONCURRENCY", "4")),
        },
        output={"format": "none", "include_links": True, "max_text_chars": 40_000},
    )
    return FetchService(
        config,
        request_timeout=REQUEST_TIMEOUT,
        queue_timeout=QUEUE_TIMEOUT,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    service = build_service()
    await service.start()
    app.state.crawler = service
    log.info("crawl service started (concurrency=%d)", CONCURRENCY)
    try:
        yield
    finally:
        # Without this the backend's connections -- or its browser -- outlive
        # the worker and leak on every reload.
        await service.close()
        log.info("crawl service stopped")


app = FastAPI(title="crawl service", version="1.0.0", lifespan=lifespan)


class FetchRequestBody(BaseModel):
    urls: list[str] = Field(..., min_length=1, description="URLs to fetch.")
    include_text: bool = True
    include_links: bool = False


class PageOut(BaseModel):
    url: str
    final_url: str
    status: str
    http_status: int | None
    title: str
    text: str | None = None
    links: list[str] | None = None
    error: str | None = None
    elapsed_s: float


@app.post("/fetch", response_model=list[PageOut])
async def fetch(body: Annotated[FetchRequestBody, Body()]) -> list[PageOut]:
    """Fetch a batch of URLs. Every URL yields a result, failures included.

    A partial failure is reported per URL rather than failing the request: the
    caller wants to know which of its URLs did not work, not to lose the batch
    to one unreachable host.
    """
    service: FetchService = app.state.crawler
    if len(body.urls) > MAX_URLS_PER_REQUEST:
        raise HTTPException(
            status_code=413,
            detail=f"at most {MAX_URLS_PER_REQUEST} urls per request",
        )

    try:
        pages = await service.fetch(body.urls)
    except ServiceBusy as exc:
        # 503 + Retry-After, so callers back off instead of piling on.
        raise HTTPException(status_code=503, detail=str(exc), headers={"Retry-After": "2"}) from exc

    return [
        PageOut(
            url=page.url,
            final_url=page.final_url,
            status=page.status,
            http_status=page.http_status,
            title=page.title,
            text=page.text if body.include_text else None,
            links=page.links if body.include_links else None,
            error=page.error,
            elapsed_s=round(page.elapsed, 3),
        )
        for page in pages
    ]


@app.get("/healthz")
async def healthz(response: Response) -> dict[str, Any]:
    """Liveness plus saturation, so a balancer can route away from a full worker."""
    service: FetchService = app.state.crawler
    stats = service.snapshot()
    saturated = stats["in_flight"] >= CONCURRENCY
    if not service.ready:
        response.status_code = 503
    return {"ready": service.ready, "saturated": saturated, **stats}


@app.get("/stats")
async def stats() -> dict[str, Any]:
    service: FetchService = app.state.crawler
    return service.snapshot()
