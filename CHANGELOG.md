# Changelog

## 0.4.0

### Changed

- **`browser.wait_until` now defaults to `load`** (was `domcontentloaded`).
  `domcontentloaded` fires before any XHR has returned, so on a client-rendered
  page it captured the shell — nav and footer, no content — and returned it as a
  fast HTTP 200 with nothing in the metrics to indicate a problem. Measured on a
  live-scores SPA: `domcontentloaded` 1.21s and **0** of the scores, `load` 2.74s
  and all 136, `networkidle` 3.98s and all of them. The wait strategy, not the
  backend, is what decides this: all three backends produce byte-identical output
  at a given setting. Pass `--wait-until domcontentloaded` for server-rendered
  targets, where it is both faster and sufficient.

### Added

- **`browser.block_prefetch`** (`--block-prefetch`) drops speculative
  navigations, identified by Chromium's own `Purpose: prefetch` header rather
  than by guesswork. A single `<link rel="prefetch">` per page doubles the HTML a
  site serves us (8 pages → 16 requests); a Next.js-like page with 20 prefetch
  links took a fixture from 56 to 105 requests over 18 pages.
  **This is a courtesy to the origin, not a speed feature** — measured wall time
  is unchanged (2.92s → 2.99s), because prefetch is issued at lowest priority
  after the page has already resolved. No Chromium flag suppresses it;
  `Prerender2`, `NetworkPrediction`, `SpeculationRules` and
  `--disable-background-networking` were all measured with no effect.
- **`browser.blocked_hosts`** (`--block-host`, repeatable) aborts requests to a
  host and its subdomains, for analytics and tag managers. Matching is by host,
  never by method: POST looks like a clean "beacon" signal and is not, since
  GraphQL content APIs are POSTs.
- **`browser.trace_resources`** (`--trace-resources`) reports requests, bytes and
  cache hit rate by resource type at the end of a crawl. Passive listeners plus
  CDP, neither of which perturbs the cache, so it is free to leave on. Both the
  prefetch storm and the cache loss below are invisible in crawl output; this is
  how you see them.
- `polycrawl.resources` (`BlockPolicy`, `ResourceTrace`, `attach_trace`), shared
  by both browser backends.
- The test site serves `/res/N` — prefetch links, a shared cacheable stylesheet
  and a cross-host beacon — and counts requests, so these properties are asserted
  from the server's side rather than inferred.

### Notes

- **Both blocking options are off by default because they are not free.**
  Registering a Playwright route disables Chromium's HTTP cache for the entire
  browser context, so shared bundles are refetched on every page: **1.63× slower**
  end to end (2.91s → 4.76s over 18 pages at 150 ms latency). This is not about
  what the handler does — `continue_()`, `fallback()`, and a pattern matching *no
  URL at all* are equally destructive. The crawlee backend logs a warning when
  either option is enabled.
- The same effect explains a gap that 0.2.0 recorded without a cause: **crawlee
  is 1.93× faster than scrapy** on the same workload (2.91s vs 5.62s).
  scrapy-playwright installs `page.route("**")` unconditionally and ends every
  request with `route.continue_()`, so it can never reuse a cached bundle —
  scrapy measures identically whether assets are cacheable or not (5.62s / 5.64s),
  where crawlee fetches a shared stylesheet once for six pages instead of six
  times. **Use crawlee when rendering.**

## 0.3.0

### Added

- **`polycrawl.service.FetchService`** — a request/response entry point, for
  putting polycrawl behind an API. One warm backend per process, callers await
  their own results, shared robots cache and per-host pacing, per-URL timeouts,
  retries for transient failures only, and bounded capacity that raises
  `ServiceBusy` (→ 503) instead of queueing without limit. `CrawlEngine` remains
  the way to run a crawl; it is the wrong shape for an API request, since it
  cannot be fed URLs while running, dedups permanently across callers, and
  carries no correlation id back to the caller.
- **`examples/fastapi_service.py`** — a complete FastAPI service: lifespan-managed
  pool, batch `/fetch` with per-URL results, 503 + `Retry-After` when saturated,
  413 on oversized batches, `/healthz` and `/stats`. Measured end-to-end at 250 ms
  target latency and `concurrency=32`: 39.5 URLs/s at p99 0.40s, and 78.8 URLs/s
  at p99 0.36s, both flat.
- **`docs/deployment.md`** — embedding vs a separate service, with nginx and
  uvicorn configuration and Little's-law sizing. The per-host limiter and robots
  cache are per process, so N load-balanced workers let one host see N times the
  configured rate; that is why a separate service is recommended.
- **`docs/js-rendering.md`** — rendering is deferred for the service path.
  Records what was measured (two of three real sites need no browser at all;
  rendered throughput is 19–35 URLs/s per process against 120–150 without) and the
  design for routing by evidence rather than by a hand-maintained site list.
- A `service` extra (`fastapi`, `uvicorn`) for running the example. `FetchService`
  itself has no web dependencies.

## 0.2.1

### Fixed

- **Resource blocking made rendered crawls fail on real sites.** Both browser
  backends now block images and fonts with Chromium launch flags instead of
  intercepting requests. Interception routed every request through Python, and
  aborting is visible to the page: a live-scores SPA re-requested the aborted
  images 16,550 times in 25s and never fired `load`, so every wait strategy
  except `domcontentloaded` timed out. With flags the same page reaches full
  content in 2.9s — faster than not blocking at all.
- **crawlee ignored `block_resources` entirely.** It was read from the config and
  never applied, so pages loaded every image and `networkidle` never arrived
  (60s timeout, now 4.0s).
- **crawl4ai turned thin pages into retryable errors.** Its anti-bot heuristic
  declares any page under 5KB with under 50 visible characters "blocked by
  anti-bot protection" even on a clean HTTP 200, which redirect stubs, empty tag
  listings and "no results" pages all trip — so the same URL was an error on
  crawl4ai and an ordinary page on the other two, and each one burned
  `max_retries` futile attempts. A clean response with HTML is now a page; the
  verdict is preserved in the page's `error` field rather than discarded.

## 0.2.0

### Added

- **scrapy backend** (`backend: scrapy`), rendering through scrapy-playwright by
  default and with `backend_options: {render: false}` for browserless HTML-only
  fetching — measured at 1.7x the throughput and 2.4x lower per-page latency on
  server-rendered pages.
- `polycrawl bench` — measures pages/s, latency percentiles and success rate per
  backend and concurrency level, with output discarded so the numbers reflect
  fetching and parsing rather than disk.
- `output.text_source` (`engine` | `backend`): whether page text is extracted
  centrally or taken from a backend's own rendering.
- CLI: `--no-render`, `--text-source`.
- `examples/` — library usage, a complete third-party backend, an annotated YAML
  config. `docs/engineering-notes.md`. GitHub Actions CI. `py.typed`.
- The test site now also serves a server-rendered link graph at `/static/N`, so a
  crawler running without a browser has something it can traverse.

### Fixed

- **crawlee: a second crawl in the same process hung** whenever it ran on a new
  event loop (a fresh `asyncio.run`, a worker rebuilding its loop,
  pytest-asyncio's per-test loop). Crawlee caches storages in a process-global
  instance manager keyed by the storage client's class name, so successive
  backends shared one request queue whose `asyncio.Lock` belonged to the first
  crawl's loop. Each instance now gets isolated storages and its own services,
  and drops them on close so a long-lived process does not retain every past
  crawl's requests.
- **Backends did not agree on page text.** The engine preferred a backend's own
  text when it had one, so crawl4ai returned markdown where the others returned
  plain text for identical HTML. Extraction is now central by default.
- **The scrapy backend launched two browsers.** Scrapy builds one download
  handler per scheme and each scrapy-playwright handler launches its own
  Chromium; both schemes now share one.
- The cross-backend equivalence test compared crawls truncated by `max_pages`,
  which is a race against fetch latency rather than a property of the crawl.

### Changed

- Cross-backend equivalence is now asserted across all three backends rather
  than a hardcoded pair.
- `ruff` covers `tests/` and `examples/` as well as `src/`.

## 0.1.0

Initial version: the engine (host-fair frontier, dedup, robots, adaptive
per-host politeness, retries, bounded-queue backpressure, selectolax extraction,
buffered JSON Lines output), the backend contract and registry, the crawl4ai and
crawlee backends, and the CLI.
