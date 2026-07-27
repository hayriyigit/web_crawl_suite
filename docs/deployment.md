# Deploying polycrawl behind FastAPI and nginx

## Use `FetchService`, not `CrawlEngine`

`CrawlEngine` runs *one crawl*: it seeds from config, owns a frontier, dedups
permanently, and finishes when that frontier drains. An API request is the other
shape — a caller arrives with its own URLs, wants them back, and the next caller
may legitimately ask for the same URL again. Four things make the engine the
wrong tool for it:

- there is no way to feed URLs into a running crawl (`run()` ends when the
  frontier empties);
- `CrawledPage` carries no correlation id, so results cannot be routed back to
  the caller that asked;
- dedup is permanent per engine, so a second caller asking for a URL already
  seen gets nothing;
- `max_pages` is a per-engine countdown, not a per-request budget.

[`polycrawl.service.FetchService`](../src/polycrawl/service.py) is the
request/response shape: one warm backend per process, callers await their own
results, shared robots cache and per-host pacing, bounded capacity that rejects
rather than queues without limit.

## Recommendation: run it as its own service

**Because the per-host rate limiter and robots cache live in process memory.**
Run the fetch pool inside N FastAPI workers and each worker gets its own limiter,
so a single target host sees up to **N times** the rate you configured. Politeness
silently degrades exactly as you scale, and the failure mode is getting blocked
by the sites you depend on. Nothing warns you.

|  | Embedded in the API workers | Separate crawl service |
|---|---|---|
| per-host rate limit | **× number of workers — wrong** | correct, one authority |
| robots.txt cache | duplicated per worker, refetched per worker | shared, fetched once |
| scaling | coupled: more API capacity = more outbound pressure | independent of API replicas |
| memory | one pool (or browser) per worker | one pool total |
| network hops | none | +1 hop (~1 ms on localhost) |
| deploy units | one | two |
| blast radius | a crawl bug takes API workers with it | isolated |

Embedding is defensible for a single-worker deployment or a prototype. Since you
are putting nginx in front specifically to scale out, separate it — the one extra
hop costs about a millisecond against fetches that take hundreds.

**If you embed anyway**, divide the per-host budget by the worker count:

```bash
# 4 workers that must together stay under 4 rps per host
CRAWL_PER_HOST_RPS=1 uvicorn app:app --workers 4
```

That is a workaround, not a fix: it wastes capacity when workers are unevenly
loaded, and it silently breaks again the day someone changes `--workers`.

## Topology

```
                    ┌──────────────────┐
   clients ── nginx ─┤ FastAPI  (N replicas, stateless, scale freely)
                    └────────┬─────────┘
                             │ POST /fetch  (internal)
                    ┌────────▼─────────┐
                    │ crawl service    │  1 replica to start.
                    │ FetchService     │  Owns robots cache + per-host limiter.
                    └────────┬─────────┘
                             │
                        the internet
```

Scale the API tier freely; it holds no crawl state. Add crawl-service replicas
only when one is saturated, and when you do, remember each replica is a separate
politeness authority — either shard by target host (consistent hashing on the
URL's host, so one host is always served by the same replica) or divide
`CRAWL_PER_HOST_RPS` by the replica count.

### nginx

```nginx
upstream api        { server 127.0.0.1:8000; server 127.0.0.1:8001; }
upstream crawl_svc  { server 127.0.0.1:8080; keepalive 32; }

server {
    listen 80;

    location / {
        proxy_pass http://api;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
    }

    # Only if the crawl service is exposed through nginx at all -- prefer
    # keeping it on the internal network and unreachable from outside.
    location /internal/crawl/ {
        proxy_pass http://crawl_svc/;
        proxy_http_version 1.1;
        proxy_set_header Connection "";

        # Fetches take seconds, not milliseconds. nginx's 60s default read
        # timeout will cut off a legitimately slow batch.
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;

        # The service already returns 503 with Retry-After when it is full;
        # do not let nginx silently retry that onto another replica and
        # multiply the load.
        proxy_next_upstream error timeout;
    }
}
```

### The crawl service

Run **one uvicorn worker per crawl-service process**, and scale by adding
processes rather than `--workers`. Workers inside one process each build their own
`FetchService` — same duplicated-limiter problem, in a place you are less likely
to look.

```bash
uv sync --extra scrapy --extra service
CRAWL_CONCURRENCY=32 CRAWL_PER_HOST_RPS=4 \
  uvicorn examples.fastapi_service:app --host 0.0.0.0 --port 8080 --workers 1
```

## Sizing

Little's law is the whole calculation:

```
concurrency ≈ target_rate × average_fetch_latency
```

At 40 URLs/s and 1s average real-world latency, you need ~40 fetches in flight.
Set `CRAWL_CONCURRENCY` a little above that and let the semaphore reject the rest.

Measured end-to-end through FastAPI, against a local site with 250 ms injected
latency, `CRAWL_CONCURRENCY=32`, no browser (12-core box also running the target):

| offered | achieved | p50 | p95 | p99 | result |
|---|---|---|---|---|---|
| 40 URLs/s | 39.5/s | 0.36s | 0.40s | 0.40s | flat, no queueing |
| 80 URLs/s | 78.8/s | 0.36s | 0.36s | 0.36s | flat, no queueing |

So one process at concurrency 32 handles 40 URLs/s with roughly 2x headroom.
Latency staying flat as the offered rate doubles is the thing to watch — it means
work is not accumulating.

**With rendering the picture is completely different**: ~19–35 URLs/s per process
depending on backend, and 3–5 GB of Chromium. Offering 40/s to a rendered pool
produced an unbounded queue: 20.2/s achieved and p99 latency of 36s, still
climbing. See [js-rendering.md](js-rendering.md).

## Operating it

- **Backpressure.** `ServiceBusy` → 503 + `Retry-After`. Never turn that into an
  unbounded queue; an overloaded pool that keeps accepting work converts one slow
  host into timeouts for every caller.
- **Timeouts.** `CRAWL_REQUEST_TIMEOUT` must be below nginx's `proxy_read_timeout`
  and your client's timeout, or callers give up while you are still working.
- **Health.** `/healthz` reports `ready` and `saturated`; `/stats` exposes the
  counters. Alert on `rejected` climbing (undersized) and on `timed_out` climbing
  (targets slow, or concurrency too high for the bandwidth).
- **Per-host limits bind before throughput does.** If many callers request the
  same host, `per_host_rps` (default 4) is the constraint, not your concurrency.
  That is deliberate. Concentrated traffic is the hard case, not volume.
- **robots.txt** costs one extra round trip per new host, then it is cached. This
  is another argument for one shared service: N processes means N lookups.
- **Never build a `FetchService` per request.** It would create a connection pool —
  or launch a browser — per call. One per process, in the lifespan hook.

## Environment variables

The [example service](../examples/fastapi_service.py) reads these, so one image
can be sized per environment:

| variable | default | meaning |
|---|---|---|
| `CRAWL_BACKEND` | `scrapy` | backend name |
| `CRAWL_CONCURRENCY` | 32 | in-flight fetches for the process |
| `CRAWL_PER_HOST_RPS` | 4 | starting per-host rate; adapts on 429/503 |
| `CRAWL_PER_HOST_CONCURRENCY` | 4 | simultaneous fetches to one host |
| `CRAWL_REQUEST_TIMEOUT` | 20 | per-URL ceiling, seconds |
| `CRAWL_QUEUE_TIMEOUT` | 5 | wait for a slot before 503 |
| `CRAWL_MAX_URLS_PER_REQUEST` | 20 | per-request batch cap |
