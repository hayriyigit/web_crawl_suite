# JavaScript rendering — measurements and the routing layer

**Status: implemented** as
[`polycrawl.routing.RoutingFetchService`](../src/polycrawl/routing.py). Every URL
is fetched without a browser first; only results that come back thin are
refetched with one. `FetchService` on its own still runs whichever mode you
configure — the routing is opt-in, and the measurements behind it are below.

## Why it is deferred

A browser costs 6–10x the latency and 3–5 GB of RAM, and most pages do not need
one. Of three real sites tested, two returned *identical* text without a browser:

| site | rendered | no browser | text similarity | needs a browser? |
|---|---|---|---|---|
| havelsan.com/en/board-members | 1.34s / 4613 B | **0.25s** / 4613 B | **1.000** | no — byte-identical |
| doviz.com | 1.80s / 18088 B | **0.29s** / 17872 B | 0.993 | no — 212 of 216 rates present |
| mackolik.com/canli-sonuclar | 2.65s / 17860 B | 0.17s / 5222 B | 0.452 | **yes** — scores arrive by XHR |

On doviz only four numbers were missing (`0.75`, `1.25`, `1.75`, `3.13` — a small
interest-rate widget). Everything else was server-rendered.

Throughput, measured end-to-end at 250 ms injected latency:

| path | sustained | p99 at 40 URLs/s offered | browser RAM |
|---|---|---|---|
| no browser | 40/s and 80/s flat, ~120–150/s ceiling | **0.40s** | 0 |
| rendered | 19–35/s per process | **36s and still climbing** | 3–5 GB |

Offering 40 URLs/s to a rendered pool achieved 20.2/s and built an unbounded
queue. A rendered service needs 2+ processes for 40/s, and each one is a browser
fleet to operate.

## Per-backend rendered ceilings

| backend | pages/s @ conc 32 | p50 | browser RSS |
|---|---|---|---|
| crawlee | **34.4** (flat to conc 96) | 0.69s | 4.4 GB |
| crawl4ai | 20.4 | 1.48s | — |
| scrapy + scrapy-playwright | 18.7 | 1.48s | 2.9 GB |

**Use crawlee if rendering.** It is ~65% faster than the other two under real
latency — the opposite of what a localhost benchmark showed (all three within
25%), because localhost is CPU-bound rather than concurrency-bound. Beyond
concurrency 32 none of them go faster; latency just grows, so 32 is the operating
point and extra capacity means extra processes.

## Settings that matter, if this is revisited

**Wait strategy.** `load` is the default. `domcontentloaded` returns the *shell*
on an XHR-driven page — mackolik gave 5.3 KB with none of the scores, as a fast
HTTP 200 that looks like a success and is invisible in the metrics. Measured cost
of getting the real content:

| strategy | mackolik | scores present |
|---|---|---|
| `--wait-until domcontentloaded` | 1.21s | **no** |
| `load` (default) | **2.74s** | yes |
| `--settle-ms 2000` | 3.04s | yes |
| `--wait-until networkidle` | 3.98s | yes |

`load` was both fastest and complete here. `networkidle` is the most patient and
the most fragile — it never arrives on a page that polls.

This is backend-independent. All three produce byte-identical output at a given
wait strategy — 5,430 characters and 0 scorelines on `domcontentloaded`, 19,179
and 136 on `load` — so a backend that seems not to render is nearly always this
setting. Use `--wait-until domcontentloaded` for server-rendered targets, where
it is both faster and sufficient.

**Resource blocking must use launch flags, not request interception.** Blocking
with `PLAYWRIGHT_ABORT_REQUEST` routes every request through Python *and* is
visible to the page: mackolik re-requested the aborted images in a loop, 16,829
route callbacks in 25s, and never fired `load`. With
`--blink-settings=imagesEnabled=false` and `--disable-remote-fonts` the same page
completes in 2.9s — faster than not blocking at all (4.7s). Both browser backends
now do this; see [engineering-notes.md](engineering-notes.md).

CSS has no flag equivalent and needs interception, which only the scrapy backend
implements. It saves little (0–14% measured) and risks content: JS that measures
layout can render less without stylesheets. Not worth it by default.

**Anything beyond images and fonts costs the HTTP cache.** Registering a route at
all — whatever it matches — disables Chromium's cache for the browser context,
which is 1.63× end to end because shared bundles stop being reused. That is free
on scrapy (scrapy-playwright always routes, so its cache is already gone) and
expensive on crawlee. It is also most of why crawlee is the faster renderer. See
[engineering-notes.md](engineering-notes.md) for the measurements behind
`block_prefetch` and `blocked_hosts`.

## The routing layer

Do not render everything, and do not maintain a hand-written list of sites that
need it. Route by evidence:

```
fetch(url) ──► no-browser fetch  ──► content check ──► good enough? ──► return
                                                            │
                                                            ▼ no
                                              browser pool (separate service)
                                                            │
                                                            ▼
                                                         return
```

1. **Always try HTTP first.** It costs ~0.3s; on half the sites measured it is
   the whole answer.
2. **Judge the result.** See the thresholds below.
3. **Escalate to a browser** only when the judgement fails. The browser tier
   starts lazily, on the first escalation — a deployment that never needs one
   never launches it.
4. **Cache the decision per host, with a TTL.** A site that needed rendering an
   hour ago almost certainly still does; skipping step 1 for it saves a round
   trip. Expire it, because sites get rebuilt.

```python
from polycrawl import CrawlConfig, RoutingFetchService

routing = RoutingFetchService.from_config(CrawlConfig(backend="scrapy", seeds=[...]))
await routing.start()                  # no browser yet
pages = await routing.fetch(urls)
print(routing.snapshot()["routing"]["escalation_rate"])
await routing.close()
```

Both tiers share one rate limiter and one robots cache. Two `FetchService`
instances with their own would let a host see twice the configured rate — the
defect [deployment.md](deployment.md) warns about across processes, reintroduced
inside one.

### Choosing the thresholds

No single signal works. Measured on four real sites, plain fetch:

| site | needs a browser? | text | text/html | script % | rendered gain |
|---|---|---|---|---|---|
| tcmb.gov.tr/…/bugun | **yes** | 1,949 | 0.062 | 18.5% | 2.32× |
| mackolik.com | **yes** | 5,222 | **0.017** | 82.9% | 3.70× |
| havelsan.com | no | 4,613 | 0.054 | 30.7% | 1.00× |
| doviz.com | no | 18,188 | 0.057 | 7.6% | 1.01× |

- A **text floor** catches tcmb (1,949 is the lowest) but not mackolik, whose
  5,222 characters of navigation exceed havelsan's real content.
- A **text-to-HTML ratio** catches mackolik (0.017) but not tcmb — whose 0.062 is
  *higher* than either site needing no browser.
- **Script share** is useless here: havelsan needs no browser at 30.7%, tcmb does
  at 18.5%.

So `ContentCheck` combines the first two with OR — `min_text=2500`,
`min_text_ratio=0.03` — which classifies all four correctly. Four sites is a small
sample and those numbers are fitted to it; treat them as a starting point.

`require_marker` is the reliable option when the caller knows what it wants. It
matches **extracted text, never HTML**, and that distinction is load-bearing: the
marker for JS-built content is usually already in the raw HTML, sitting inside the
script that has not run yet. Matching HTML would report success on exactly the
page that needs a browser. Any callable works too, so a price scraper and a text
indexer can disagree about "good enough".

### Sizing consequence

The browser tier is small. If 20% of URLs escalate, 40 URLs/s means ~8/s
rendered — one process, not a fleet. `escalation_rate` in `snapshot()` is the
number that tells you whether it is sized right; watch `escalation_failed` and
`rendered_not_better` alongside it.

Measured end to end, defaults, four real sites:

| site | route taken | time |
|---|---|---|
| havelsan | plain | 0.28s |
| doviz | plain | 0.44s |
| tcmb | escalated (`thin-text:1949`) | 1.14s |
| mackolik | escalated (`low-text-ratio:0.017`) | 2.56s |

Escalation rate 0.5 on that mix, and the tcmb page comes back with the exchange
rate table that a browserless fetch silently omits.

## Reproducing any of this

```bash
# per-backend throughput under realistic latency
python slow_site.py --port 8740 --pages 4000 --latency-ms 250   # see engineering-notes
uv run polycrawl bench http://127.0.0.1:8740/ --backends crawl4ai,crawlee,scrapy -j 32

# whether a site actually needs a browser
uv run polycrawl crawl <url> -b scrapy -n 1 --max-depth 0 -o /tmp/rendered.jsonl
uv run polycrawl crawl <url> -b scrapy -n 1 --max-depth 0 --no-render -o /tmp/plain.jsonl
# compare the text field: if they match, the browser is buying you nothing
```
