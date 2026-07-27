# Changelog

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
