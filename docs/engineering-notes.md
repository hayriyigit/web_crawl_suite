# Engineering notes

Traps found while building this, kept because each one cost real time and none of
them is obvious from the libraries' documentation. If you are changing a backend,
read the section for it first.

## Cross-cutting

### `await asyncio.gather(*tasks)` on already-finished tasks does not yield

```python
while self._tasks:                       # WRONG: infinite tight loop
    await asyncio.gather(*self._tasks)
```

Awaiting a *completed* task does not yield to the event loop, so the
`add_done_callback` that removes it from the set never gets to run. The loop spins
forever at 100% CPU with no timers firing and no cancellation possible. It hung
the entire test suite once.

```python
while pending := [t for t in self._tasks if not t.done()]:   # right
    await asyncio.gather(*pending, return_exceptions=True)
```

Present correctly in `crawl4ai_backend.drain`, `tests/conftest.py::FakeBackend`
and `examples/custom_backend.py`. Watch for it anywhere a set of tasks is drained.

### Library state that is bound to an event loop is a landmine

Two of the three backends wrap libraries that keep process-global state
containing `asyncio.Lock` objects. A lock belongs to the loop that created it, so
any second crawl on a *different* loop dies with `... is bound to a different
event loop`. Different loops are not exotic: a second `asyncio.run`, a worker
thread that rebuilds its loop, or pytest-asyncio, which by default gives every
test a fresh one.

If you add a backend over a library with module-level singletons, test it by
running two crawls in two `asyncio.run` calls. One crawl passing proves nothing.

### `Counter[missing]` returns 0 without inserting

So `del counter[host]` raises `KeyError` for a host that was never counted. Use
`.get()` / `.pop(key, None)`. `engine._release` also tolerates a double release
deliberately: a backend that emits twice for one request must not be able to
wedge in-flight accounting.

## crawlee

### Storages are cached process-globally, keyed by the client's class name

`RequestQueue.open()` resolves through `service_locator.storage_instance_manager`,
which is a **class attribute** — every `ServiceLocator` instance shares it. The
default cache key is the storage client's class name, so two `CrawleeBackend`
instances resolve to the *same* cached request queue, whose `asyncio.Lock` is
bound to the first crawl's loop. Symptom: the second crawl hangs with every
request outstanding, after logging

```
<asyncio.locks.Lock object at 0x... [locked]> is bound to a different event loop
```

The queue's `_opener_locks` entries are a second instance of the same trap.

The fix in `_isolated_storage_client()` is a `MemoryStorageClient` subclass that
returns a per-instance cache key, plus passing `configuration`, `storage_client`
and `event_manager` explicitly so `BasicCrawler` builds its own `ServiceLocator`
rather than reaching for the global one. Because the cache is global, `close()`
must drop what it opened or every past crawl's requests stay in memory.

`use_session_pool=False` for the same reason: `SessionPool` resolves its
persisted state through the *global* service locator, which would reintroduce
exactly the shared state the rest of the wiring avoids.

### Latency has to be measured from submit

Crawlee navigates *before* invoking the request handler, so timing from inside
the handler measures only `page.content()` and reports a p50 near 0.003s.

## scrapy

### The Twisted reactor is installed once per process and bound to one loop

`install_reactor()` binds the asyncio reactor to whatever
`asyncio.get_event_loop()` returns, suppresses `ReactorAlreadyInstalledError`,
and there is no way to rebind or replace it afterwards. Installing it on the
caller's loop therefore breaks every subsequent crawl on a new loop, and it
decides a process-global for the host application as a side effect.

Hence `_reactor_loop()`: a private loop on a daemon thread, created at most once,
with results crossing back to the engine's loop. The hand-off is awaited so the
engine's bounded result queue still applies backpressure through the thread
boundary. Importing the module installs nothing — a test asserts this.

### Scrapy connects signal receivers weakly

```python
crawler.signals.connect(local_function, signal=spider_idle)   # collected!
```

`SignalManager.connect` passes through to pydispatcher, which defaults to
`weak=True`. A closure with no other reference is garbage collected as soon as the
enclosing frame returns, the receiver silently disappears, and the spider closes
on its first idle. Symptom: the first batch succeeds and every later `submit`
raises `No open spider to crawl`. Use a module-level receiver and `weak=False`.

### The spider must be *open*, not merely created

`crawl_async()` sets `crawler.engine` and `crawler.spider` before
`open_spider_async()` completes, and `engine.crawl()` rejects requests until the
spider is open. Poll `crawler.engine.spider is not None`.

### One download handler per scheme means one browser per scheme

`ScrapyPlaywrightDownloadHandler` sets `lazy = False`, so scrapy instantiates it
eagerly for *every* registered scheme — and each instance launches its own
Chromium. The configuration scrapy-playwright's README documents (`http` and
`https`) therefore costs two browsers per crawl, roughly double what the other
backends use. Both schemes must stay registered, or pages reached over the other
scheme would silently skip JavaScript, so `_shared_playwright_handler()` makes
both resolve to one instance with an idempotent `close()`.

Verify with `pgrep -af headless` filtered to processes without `--type=`
(everything with `--type=` is a renderer child, not a browser).

## Resource blocking: use browser flags, not request interception

Blocking images/fonts/media by intercepting requests (`page.route`,
scrapy-playwright's `PLAYWRIGHT_ABORT_REQUEST`) looks like the obvious
implementation and is a trap twice over.

Every request — not only the blocked types — crosses into Python for the
predicate. Worse, aborting is *visible to the page*: a site whose script retries
images that fail will spin. Measured on a real live-scores SPA, one page produced

```
route calls=16829  by type={'image': 16550, 'script': 94, 'fetch': 126, ...}
routing window=24.99s
```

and never fired its `load` event, so every wait strategy except
`domcontentloaded` timed out. Blocking the same types with launch flags took the
page to full content in 2.9s — *faster* than not blocking at all (4.7s), which is
what blocking is supposed to do.

```
--blink-settings=imagesEnabled=false    # image
--disable-remote-fonts                  # font
```

The requests are never issued, so there is nothing to retry and nothing to route.
Both browser backends now do this (`_blocking_args`, `_playwright_settings`).
`media` has no flag equivalent and is left alone rather than paying interception
for one request in a few hundred. Non-chromium browsers fall back to
interception, since the flags are Blink-specific.

The same defect had a quieter form in the crawlee backend: `block_resources` was
accepted from the config and then *silently ignored*, so pages loaded every image
and `networkidle` never arrived (60s timeout, now 4.0s).

### Registering *any* route disables the browser's HTTP cache

The reason interception is expensive turned out to be much larger than the
per-request Python hop. Measured with a fixture that counts requests server-side,
six pages sharing one `max-age=3600` stylesheet:

| handler | origin served the CSS |
|---|---|
| no route | **1×** |
| `route → continue_()` | 6× |
| `route → fallback()` | 6× |
| route whose pattern matches **nothing** | 6× |

The last row is the finding. It is not about what the handler does or which URLs
it matches — merely *registering* a route on a browser context turns off
Chromium's HTTP cache for that whole context, so every shared bundle is refetched
on every page. End to end at 150 ms latency over 18 pages: **2.91s → 4.76s,
1.63×**.

Two consequences shape the design in `polycrawl/resources.py`:

- Blocking that a launch flag cannot express is **opt-in** (`block_prefetch`,
  `blocked_hosts`), and `BlockPolicy.needed` exists so a default crawl never has
  a route installed.
- It is free on the scrapy backend and costly on crawlee, because
  scrapy-playwright installs `page.route("**")` unconditionally
  ([`handler.py`][sp-route]) and ends every request with `route.continue_()`.
  Its cache is already forfeit — which is most of why crawlee is 1.93× faster on
  the same workload (2.91s vs 5.62s) and why scrapy measures identically whether
  the assets are cacheable or not (5.62s / 5.64s).

[sp-route]: https://github.com/scrapy-plugins/scrapy-playwright

### Observation is free; CDP reports cache disposition, `server_addr` does not

`page.on("request")` / `page.on("response")` listeners do **not** disturb the
cache (verified against the same fixture: still 1 CSS fetch for 6 pages), so
`trace_resources` is safe to leave on. But those events count what the *page
asked for*, including requests answered from cache — the trace reported 6
stylesheet requests where the origin served 1, which would hide exactly the
regression above.

Getting the true disposition needs CDP, and the obvious signals do not work:
`response.server_addr()` returns the original address for cache hits too, and
`timing.connectStart` is `-1` for any reused connection. What works is
`Network.responseReceived` with `fromDiskCache`, plus
`Network.requestServedFromCache` for the memory tier. A request can appear in
both, so disposition is resolved once per `requestId`.

Two traps when wiring it up, both of which fail silently as "0 cache hits":

- The `CDPSession` must be kept alive. It was a local in `attach_trace`, so
  Python collected it and took its listeners with it.
- Handling only `requestServedFromCache` misses disk-cache hits entirely — those
  arrive as `responseReceived(fromDiskCache=True)` and must be counted there.

### Speculative prefetch is a cost to the origin, not to us

`<link rel="prefetch">` roughly doubles the HTML a site serves a rendering
crawler: 8 real navigations produced 16 HTML requests, and a Next.js-like page
with 20 prefetch links took a fixture from 56 to 105 requests over 18 pages. None
of it costs us wall time — 2.92s → 2.99s — because prefetch is issued at lowest
priority *after* the page has resolved, by which point the crawler has extracted
its text and moved on. The same holds for analytics beacons (2.96s → 2.94s with
the beacon removed).

So `block_prefetch` is a courtesy feature, and is documented as one. No Chromium
flag suppresses prefetch — `Prerender2`, `NetworkPrediction`, `SpeculationRules`
and `--disable-background-networking` were all measured with no effect — so it
needs a route, which is what makes it cost the cache on crawlee.

The requests do self-identify: Chromium sends `Purpose: prefetch` (and
`Sec-Purpose` for the Speculation Rules API), which is the page declaring the
request is not needed yet. That makes the rule exact rather than heuristic.

Blocking analytics is done by **host**, never by method. POST looks like a clean
signal for "beacon" and is not: GraphQL content APIs are POSTs, and the test
fixture's own body text arrives over an XHR.

## crawl4ai

- `BrowserConfig(user_agent=None)` crashes: it derives client hints and regexes
  the UA. Build kwargs conditionally and omit `user_agent`, `proxy` and
  `max_pages_before_recycle` when unset.
- It produces markdown natively, which is *not* the default output — see below.

### Its anti-bot heuristic fails thin pages

`is_blocked()` runs unconditionally in `AsyncWebCrawler` — there is no setting to
turn it off — and its tier-3 structural check flags a page when it is under 50KB
and either scores two signals, or scores one while being under 5KB. "Under 50
visible characters" is one such signal, so **any page under 5KB with under 50
characters of text is reported as "Blocked by anti-bot protection"**, on a clean
HTTP 200 with the HTML present.

Redirect stubs, empty tag listings and "no results" pages all trip it. Two
consequences: the same URL was an error on crawl4ai and an ordinary page on
crawlee and scrapy, and because `BACKEND_ERROR` is retryable each one burned
`max_retries` attempts that could not possibly succeed.

`_to_result` now treats a sub-400 status with non-empty HTML as a page and keeps
crawl4ai's reason in `error`, so the signal is visible without the backend
deciding crawl policy. `tests/test_backend_mapping.py` pins this, including that a
genuine failure — no status, no HTML — is still a failure.

## Extraction and parity

### selectolax: `strip_tags`, not `unwrap_tags`

`unwrap_tags` keeps an element's children, so `<script>`/`<style>` bodies leak
into extracted text. `tree.strip_tags(...)` removes the element with its content.

The strip list is deliberately narrow: `<nav>`, `<header>` and `<footer>` are
*kept*, because a general-purpose crawler should not silently drop content that
may be the only thing on the page.

### Backend-native text is opt-in

The engine used to prefer `result.text` whenever a backend supplied one, which
meant crawl4ai returned markdown (`# Heading`, `[text](url)`) where crawlee and
scrapy returned plain text — for identical HTML. Backends were not
interchangeable, which is the one property the architecture exists to provide.

`output.text_source` now defaults to `engine` (extracted centrally, identical
everywhere); `backend` opts into native rendering where a backend offers it.

### A truncated crawl cannot be compared across backends

`max_pages` keeps whichever URLs were discovered first, and discovery order is a
function of fetch latency. The equivalence tests therefore use a budget high
enough to exhaust the frontier, so the result is determined by the link graph
alone. Asserting set equality on a truncated crawl is testing a race.

## Working practices that paid off

- Verify library APIs by introspection before coding against them.
  `uv run python -c "import inspect; print(inspect.getsource(...))"` caught
  `unwrap_tags`, `user_agent=None`, crawlee's handler timing, scrapy's weak
  signals and the per-scheme handler — every one of which was a wrong assumption
  that would have shipped.
- Piping pytest into `head`/`grep` swallows output and makes a hang look silent.
  Redirect to a file: `timeout 200 uv run pytest ... > /tmp/out.log 2>&1`.
- Reproduce a suspected state-contamination bug in a standalone script before
  fixing it. The crawlee bug looked like "four tests fail together but pass in
  isolation"; the actual trigger was the *event loop* changing, not the run count,
  and three runs in one loop passed cleanly.
