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

The same reasoning applies to running the whole app in N containers, which is the
usual shape in practice — see
[N replicas in Docker behind nginx](#n-replicas-in-docker-behind-nginx).

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

## N replicas in Docker behind nginx

The common plan is to skip the split above and run the whole FastAPI app —
polycrawl embedded — in four containers with nginx in front. That works. Two
things decide whether it works *well*.

### It is not what buys you throughput

One process already handles more than the usual target. Measured end to end (see
[Sizing](#sizing)): **39.5 URLs/s at p99 0.40s**, and **78.8 URLs/s at p99 0.36s**
with latency still flat. And the box is not CPU-bound — at concurrency 32 the
crawl used **13% of 12 cores**, and taking it down to 6 cores changed the wall
time not at all:

| cores | wall | CPU utilisation |
|---|---|---|
| 12 | 3.75s | 13% |
| 6 | 3.75s | 28% |
| 3 | 4.06s | 45% |

So replicas are worth running for **resilience, rolling deploys and isolation**,
not for speed. Expect no throughput change from the second container, and size
the rest of the stack accordingly. (This is also why a faster browser build —
Thorium and friends — buys nothing: there is no CPU shortage to relieve.)

### Every replica is a separate politeness authority

This is the part that bites. `HostLimiter` and `RobotsCache` are per
`FetchService`, so four containers are four independent limiters that never see
each other. Configure `per_host_rps: 4` and a popular target actually receives
**4 × 4 = 16 requests/s**, plus 16 concurrent connections and four separate
`robots.txt` fetches.

The failure mode is not a crash. It is that the sites you depend on start rate
limiting or blocking you, and nothing in your own metrics says why. It is most
likely exactly where you would least like it: several users searching the same
popular site at once, their requests landing on different containers.

**Divide the per-host budget by the replica count**, and treat that as part of
the replica count itself:

```yaml
# docker-compose.yml -- identical for all four services
environment:
  CRAWL_PER_HOST_RPS: "1"            # 4 replicas × 1 = 4/s per host overall
  CRAWL_PER_HOST_CONCURRENCY: "1"    # 4 replicas × 1 = 4 concurrent
  CRAWL_CONCURRENCY: "16"            # per container; 64 in total
  CRAWL_BACKEND: "scrapy"
```

Scaling to eight containers without halving these again doubles the load you put
on every target. Put the arithmetic in a comment next to `replicas:`, because
nothing enforces it. Sharding by target host (consistent hashing on the URL's
host, so one host is always served by the same replica) is the version that
survives a replica-count change; divide-by-N is the version you can ship today.

### nginx for this topology

```nginx
upstream crawl {
    least_conn;                 # not round-robin: see below
    server crawl1:8000;
    server crawl2:8000;
    server crawl3:8000;
    server crawl4:8000;
}

location /fetch {
    proxy_pass http://crawl;
    proxy_http_version 1.1;
    proxy_set_header Connection "";

    proxy_read_timeout 30s;     # must exceed CRAWL_REQUEST_TIMEOUT (20s)
    proxy_next_upstream error timeout;   # deliberately no http_503
}
```

`least_conn` rather than round-robin because request cost varies by more than an
order of magnitude here: `/fetch` takes anything from one URL to
`CRAWL_MAX_URLS_PER_REQUEST` of them. Round-robin counts those as equal and piles
work onto a container that is already busy.

Leaving `http_503` out of `proxy_next_upstream` is deliberate too. A 503 from
this service means *the pool is full* — retrying it against another replica does
not find spare capacity, it just multiplies load on a system that is already
saying stop. Let the 503 and its `Retry-After` reach the client.

### Docker specifics

- **One uvicorn worker per container** (`--workers 1`). Workers inside a process
  each build their own `FetchService`, which is the same duplicated-limiter
  problem one level further down, where you are less likely to look for it.
- **Memory, without a browser:** ~300–400 MB per container. Four is comfortable.
- **Memory, with rendering:** 3–5 GB *per container* for Chromium. Four replicas
  is 12–20 GB and will not fit on a 16 GB box. If you enable rendering, make the
  browser tier separate and smaller rather than multiplying it by the API replica
  count — see [js-rendering.md](js-rendering.md).
- **`shm_size` is required if you render.** Docker's default 64 MB `/dev/shm` is
  not enough for Chromium and produces random tab crashes that look like flaky
  sites:
  ```yaml
  shm_size: 1gb
  ```
- **Health checks:** `/healthz` answers 503 only while the service is not
  `ready`, which is what a container healthcheck wants — a replica still warming
  up should not receive traffic. Saturation is reported in the body
  (`"saturated": true`) but **still returns 200**, so a plain HTTP healthcheck
  will not route away from a full replica. That is intentional: a saturated
  worker is working, not broken, and taking it out of rotation would move its
  load onto replicas that are equally full. If you do want a balancer to avoid
  full replicas, read the body rather than the status code — `least_conn` above
  already approximates it.

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
