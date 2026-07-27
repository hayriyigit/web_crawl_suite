# polycrawl

A high-throughput async web crawler where **the crawl pipeline is fixed and only the fetcher is pluggable**.

The engine owns the frontier, deduplication, robots.txt, per-host politeness, retries, link extraction and output. A *backend* does one thing: turn a URL into HTML. Three ship in the box — [crawl4ai](https://github.com/unclecode/crawl4ai), [crawlee](https://github.com/apify/crawlee-python) and [scrapy](https://scrapy.org/) — and you can add your own from your own package without touching this repository.

Because everything except fetching is centralised, swapping backends changes *how pages are fetched* and nothing about what a crawl produces. That is enforced by tests: all three backends must discover the same pages and return byte-identical extracted text for the same site.

```python
import asyncio
from polycrawl import CrawlConfig, crawl

metrics = asyncio.run(crawl(CrawlConfig(
    seeds=["https://example.com"],
    backend="scrapy",        # or "crawlee", "crawl4ai", or your own
    concurrency=32,
    max_pages=500,
)))
print(metrics.summary())
```

---

## Install

Requires Python 3.11–3.13.

```bash
# into another project (uv)
uv add "polycrawl[scrapy] @ git+https://github.com/hayriyigit/web_crawl_suite"

# into another project (pip)
pip install "polycrawl[scrapy] @ git+https://github.com/hayriyigit/web_crawl_suite"

# all three backends
uv add "polycrawl[all] @ git+https://github.com/hayriyigit/web_crawl_suite"
```

The core install pulls in no browser; choose backends with extras: `crawl4ai`, `crawlee`, `scrapy`, or `all`. Any rendering backend needs Chromium once:

```bash
playwright install chromium --with-deps
```

Working on polycrawl itself:

```bash
git clone https://github.com/hayriyigit/web_crawl_suite && cd web_crawl_suite
uv sync --extra all
uv run playwright install chromium --with-deps
uv run pytest -m "not integration"   # fast: no browsers
uv run pytest                        # everything, real browsers
```

## Command line

```bash
polycrawl backends                                          # what is installed and usable
polycrawl crawl https://example.com -n 500 -j 32            # crawl
polycrawl crawl https://example.com -b scrapy --no-render    # no browser: much faster
polycrawl crawl --config examples/crawl.yaml                 # everything from a file
polycrawl config > crawl.yaml                                # template with all defaults
polycrawl bench https://example.com -j 8,16,32                # measure pages/s per backend
```

A live panel shows pages/s, in-flight fetches, frontier depth and latency percentiles. The first Ctrl-C finishes in-flight pages and writes a summary; a second aborts. Output is JSON Lines, one object per page.

## Backends

| backend | fetches with | JavaScript | notes |
|---|---|---|---|
| `crawl4ai` | Playwright + `MemoryAdaptiveDispatcher` | yes | also produces markdown natively (`text_source: backend`) |
| `crawlee` | Playwright via `PlaywrightCrawler` | yes | one warm browser pool for the whole crawl |
| `scrapy` | Twisted downloader, optionally scrapy-playwright | yes, or off | `render: false` drops the browser entirely |

### Which is fastest

Measured with `polycrawl bench` against a local 600-page site, 200 pages per run, `domcontentloaded`, resource blocking on. All runs at 100% success:

| backend | pages/s @ 16 | pages/s @ 32 | p50 latency @ 32 |
|---|---|---|---|
| crawl4ai | 46.7 | 51.5 | 0.478s |
| crawlee | 50.8 | **64.9** | 0.323s |
| scrapy (rendered) | 45.6 | 57.3 | 0.348s |

The three rendered backends land within ~25% of each other, which is the expected result: they all drive the same Chromium, so the browser dominates rather than the integration around it. Reproduce with `polycrawl bench <url> --backends crawl4ai,crawlee,scrapy -n 200 -j 16,32`.

**The real speed decision is whether you need a browser at all.** Same crawler, same site, server-rendered pages so both modes can traverse it:

| mode | pages/s @ 16 | pages/s @ 32 | p50 latency @ 32 |
|---|---|---|---|
| `scrapy` rendered | 46.0 | 58.0 | 0.338s |
| `scrapy` with `render: false` | 58.3 | **96.2** | 0.142s |

1.7x the throughput, 2.4x lower per-page latency, and no browser process at all. That is measured on a local server which is *itself* a bottleneck, so against a real site — where a browser must also execute the page's JavaScript — the gap is wider. Check whether your target actually needs rendering before paying for it.

### Why scrapy-playwright and not Splash

For scrapy's rendering layer the practical options are scrapy-playwright, scrapy-splash and scrapy-selenium. Playwright wins here on three counts:

- **Splash renders with QtWebKit**, an engine frozen years before the frameworks it would have to run, and it needs a separate service deployed alongside the crawler. It is also effectively unmaintained.
- **scrapy-selenium** is synchronous and unmaintained; it blocks the Twisted reactor.
- **scrapy-playwright** is the actively maintained `scrapy-plugins` project and — decisively for this design — drives the *same* Playwright Chromium as the other two backends. That is what allows all three to produce identical pages; a different engine would break cross-backend equivalence by construction.

Splash is lighter *per rendered page* than Chromium, but that advantage is irrelevant next to `render: false`, which removes the browser altogether and is faster than either.

## Writing a backend

Four methods. The engine does the rest.

```python
from polycrawl import CrawlerBackend, FetchResult, FetchStatus, register

@register
class MyBackend(CrawlerBackend):
    name = "mine"

    async def start(self, emit):        # browser launch, connection pools
        self._emit = emit

    async def submit(self, batch):      # queue work and RETURN; do not await fetches
        for req in batch:
            ...
            await self.emit(FetchResult(request=req, status=FetchStatus.OK, html=html))

    async def drain(self):              # block until everything submitted was emitted
        ...

    async def close(self):              # must be safe after a failed start
        ...
```

`submit` must not block until its fetches finish — it hands work off and returns, and results come back through `emit` in completion order. The interface is submit/emit rather than request→response because every serious fetching library batches and schedules internally; a one-URL-at-a-time API would throw away their dispatchers and their browser reuse.

Three registration routes, checked in order: the built-ins, entry points, then runtime registration.

```toml
# in your own package -- polycrawl then finds it by name, no fork needed
[project.entry-points."polycrawl.backends"]
mine = "my_package.backend:MyBackend"
```

See [examples/custom_backend.py](examples/custom_backend.py) for a complete working httpx backend, and `tests/conftest.py` for the reference `FakeBackend` the engine tests run against with no browser.

## JavaScript

`domcontentloaded` is the default on every rendering backend. It fires once the HTML is parsed and synchronous scripts have run, which is enough for client-rendered content, while `load` and `networkidle` also wait on images, fonts, analytics beacons and long-poll connections that may never settle.

For frameworks that hydrate a tick later:

```bash
polycrawl crawl https://spa.example.com --wait-for "#app .loaded" --settle-ms 250
```

**Check that you actually captured the content**, because a fast crawl of empty
shells looks exactly like a fast crawl. On one real live-scores SPA every backend
"opened" the page in ~1s and returned 5.3KB with none of the scores in it: the
data arrives by XHR after DOM-ready. `--settle-ms 2000` or `--wait-until load`
returned 17.4KB with all of it, in under 3s. Compare text size against
`--no-render` on a couple of pages — if a browser is not getting you more than
plain HTTP does, it is only costing you.

This is verified rather than assumed. The test site injects its title, body text and links *only* at `DOMContentLoaded`, so a backend that does not really render fails the suite. `render: false` is held to the opposite assertion — that it does *not* see the JS-injected content — so the mode switch is proven to be a real switch.

## Tuning for load

| knob | raise it when | cost of raising |
|---|---|---|
| `concurrency` | the network is idle and hosts are not complaining | memory — browsers are expensive per in-flight page |
| `batch_size` | the backend's own scheduler is underfed | slower reaction to rate limits |
| `pipeline_depth` | one slow page stalls a whole batch | more pages in flight than `concurrency` alone suggests |
| `processor_workers` | pages are large and parsing lags fetching | CPU |
| `politeness.per_host_rps` | you own the target, or have permission | getting blocked |
| `politeness.per_host_concurrency` | the target is a large site with fast pages | same |

Structural choices that matter more than any single number:

- **The frontier is host-fair, not FIFO.** Per-host queues served round-robin, with a min-heap of hosts that are cooling down. A global FIFO holds long runs of a single host, so per-host politeness would throttle the whole crawl.
- **The result queue is bounded.** When parsing falls behind, `emit` blocks and fetching slows on its own. Fetch and parse budgets (`concurrency` vs `processor_workers`) are separate, so a burst of large pages does not also throttle the network.
- **`block_resources`** drops images, media and fonts at the network layer — typically 60–80% fewer bytes for text crawling, and the single largest win.
- **`dedup: bloom`** trades a tunable false-positive rate for fixed memory on very large crawls.
- **Memory pressure pauses dispatch**, measured from `/proc/meminfo` rather than process RSS, because browsers live in child processes and RSS understates badly.
- selectolax/lexbor for parsing (~10x lxml on real pages), orjson with buffered writes off the event loop, uvloop when available.

## Output

One JSON object per line:

```json
{"url": "https://example.com/", "final_url": "https://example.com/", "depth": 0,
 "parent": null, "http_status": 200, "status": "ok", "title": "Example",
 "text": "...", "links": ["..."], "n_links": 12, "error": null,
 "elapsed_s": 0.31, "backend": "scrapy", "fetched_at": 1769500000.0}
```

`text` is extracted centrally by default, so it is identical on every backend. Set `output.text_source: backend` to use a backend's own rendering instead — crawl4ai's markdown, for example — at the cost of that comparability.

## Configuration

Every field can be set three ways: `CrawlConfig(...)` in Python, a YAML file (`--config`), or the environment with a `POLYCRAWL_` prefix (`POLYCRAWL_CONCURRENCY=64`; nested keys use a double underscore, `POLYCRAWL_BROWSER__WAIT_UNTIL=load`). [examples/crawl.yaml](examples/crawl.yaml) documents every group, and `polycrawl config` prints the current defaults.

## Architecture

```
frontier ──► scheduler ──► backend.submit()   (bounded by `concurrency`)
                ▲                │
                │                ▼ backend.emit()
          enqueue links   result queue (bounded → backpressure)
                │                │
                └──── processor workers ────► sink
```

| module | role |
|---|---|
| `engine.py` | the orchestrator: scope, budgets, retries, accounting |
| `frontier.py` | per-host queues, round-robin, deferred-host min-heap |
| `backend.py` | the `CrawlerBackend` contract and `BackendCapabilities` |
| `registry.py` | lazy discovery: built-ins, entry points, runtime |
| `ratelimit.py` | per-host token bucket with AIMD; honours `Retry-After` and `Crawl-delay` |
| `robots.py` | protego, per-host locked fetch, fails open except on 401/403 |
| `dedup.py` | exact 64-bit digests, or a Bloom filter for fixed memory |
| `extract.py` | selectolax: links, title, text, canonical and meta-robots in one parse |
| `sinks.py` | `Sink` contract, JSON Lines (plain or gzip), memory, null |
| `memory.py` | system memory pressure from `/proc/meminfo` |
| `metrics.py` | counters, sliding-window throughput, latency percentiles |

Backends are imported lazily — a test asserts that importing the registry pulls in none of crawl4ai, playwright, scrapy or twisted — so having one backend installed and not the others never breaks start-up. An unavailable backend reports why.

## Notes on the scrapy backend

Scrapy is Twisted-based, and a Twisted reactor is installed **once per process** and bound to one event loop permanently. Installing it on the caller's loop would mean a second crawl — a new `asyncio.run`, a worker rebuilding its loop, a test suite with a loop per test — inheriting a reactor pinned to a dead loop. So the reactor gets a private loop on its own thread and results cross back to the engine's loop; that hand-off is awaited, which is what keeps backpressure working through the thread boundary. Importing the backend installs nothing, which a test also asserts.

Scrapy's own retry, robots, autothrottle and depth limits are switched off so the engine stays the single authority on what gets fetched and when. Both URL schemes share one browser: scrapy builds a download handler per scheme and each scrapy-playwright handler otherwise launches a Chromium of its own.

## Status

133 tests pass (107 unit, 26 integration against real browsers); `ruff check` clean.

Deliberately not implemented, and reasonable next steps: crawl resume/checkpointing, screenshot capture, `sitemap.xml` seeding, and sinks for destinations other than files.

## License

MIT
