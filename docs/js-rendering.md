# JavaScript rendering — measurements and a routing design (deferred)

**Status: not implemented.** The service path runs `render: false` — no browser.
This records what was measured, so the decision can be revisited without redoing
the work.

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

**Wait strategy.** `domcontentloaded` is the default and returns the *shell* on an
XHR-driven page — mackolik gave 5.3 KB with none of the scores, and looked like a
fast success. Measured cost of getting the real content:

| strategy | mackolik | scores present |
|---|---|---|
| `domcontentloaded` | 1.21s | **no** |
| `--wait-until load` | **2.74s** | yes |
| `--settle-ms 2000` | 3.04s | yes |
| `--wait-until networkidle` | 3.98s | yes |

`load` was both fastest and complete here. `networkidle` is the most patient and
the most fragile — it never arrives on a page that polls.

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

## The routing design, if rendering is added

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

1. **Always try HTTP first.** It costs ~0.25s; on two of three sites it is the
   whole answer.
2. **Check whether the result is plausible.** Cheap signals, in rough order of
   reliability: extracted text length below a floor (~500 chars); a body that is
   mostly `<script>`; a known SPA root (`<div id="__nuxt">`, `<div id="root">`)
   with little text; a caller-supplied CSS selector that did not match.
   crawl4ai's own heuristic — under 5 KB of HTML *and* under 50 visible
   characters — is a reasonable starting point, though it false-positives on
   genuinely thin pages, which is why polycrawl no longer treats its verdict as a
   failure.
3. **Escalate to a browser pool** only on failure, with `wait_until=load`.
4. **Cache the decision per host, with a TTL.** A site that needed rendering an
   hour ago almost certainly still does; skipping step 1 for it saves a round
   trip. Expire it, because sites get rebuilt.

Sizing consequence: the browser tier is small and separate. If 20% of URLs
escalate, 40 URLs/s means ~8/s rendered — one process, not a fleet.

### What implementing it would take

- A `RoutingFetchService` wrapping two `FetchService` instances (one `render:
  false`, one rendering), with the content check and the per-host TTL cache.
- A pluggable `sufficient(page) -> bool` predicate, since "good enough" is
  caller-specific — a price scraper and a text indexer disagree.
- Metrics for the escalation rate, which is the number that tells you whether the
  browser tier is sized right.
- Tests: a fixture site with a JS-only page and a server-rendered one (the
  existing test site already serves both — `/page/N` and `/static/N`), asserting
  the JS-only page escalates and the static one does not.

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
