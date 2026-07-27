"""Command-line interface."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.live import Live
from rich.logging import RichHandler
from rich.table import Table

from .config import CrawlConfig
from .engine import CrawlEngine
from .metrics import Metrics
from .registry import list_backends

app = typer.Typer(
    name="polycrawl",
    help="Pluggable high-throughput web crawler with swappable fetch backends.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console(stderr=True)


def _setup_logging(verbose: bool, quiet: bool) -> None:
    level = logging.DEBUG if verbose else logging.ERROR if quiet else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=verbose)],
    )
    # These libraries are chatty at INFO and drown out our own progress output.
    for noisy in ("crawlee", "crawl4ai", "httpx", "asyncio", "playwright", "scrapy", "twisted"):
        logging.getLogger(noisy).setLevel(logging.WARNING if not verbose else logging.INFO)


def _install_uvloop() -> str:
    """Swap in uvloop when present -- typically 2-4x less event-loop overhead."""
    try:
        import uvloop
    except ImportError:
        return "asyncio"
    uvloop.install()
    return "uvloop"


@app.command()
def crawl(
    seeds: Annotated[
        list[str] | None,
        typer.Argument(help="Seed URLs. Omit when --config supplies them."),
    ] = None,
    config_file: Annotated[
        Path | None, typer.Option("--config", "-c", help="YAML config file.")
    ] = None,
    backend: Annotated[
        str, typer.Option("--backend", "-b", help="Fetch backend (see `polycrawl backends`).")
    ] = "crawl4ai",
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Output file (.jsonl or .jsonl.gz). Default: stdout."),
    ] = None,
    concurrency: Annotated[
        int, typer.Option("--concurrency", "-j", min=1, help="Max simultaneous fetches.")
    ] = 32,
    max_pages: Annotated[
        int, typer.Option("--max-pages", "-n", min=1, help="Stop after this many pages.")
    ] = 1000,
    max_depth: Annotated[
        int, typer.Option("--max-depth", "-d", min=0, help="Link depth from the seeds.")
    ] = 2,
    scope: Annotated[
        str,
        typer.Option("--scope", help="site | host | path | any -- how far links may wander."),
    ] = "site",
    batch_size: Annotated[
        int, typer.Option("--batch-size", min=1, help="URLs handed to the backend per submit.")
    ] = 64,
    pipeline_depth: Annotated[
        int, typer.Option("--pipeline-depth", min=1, help="Concurrent batches in flight.")
    ] = 2,
    workers: Annotated[int, typer.Option("--workers", min=1, help="HTML parsing workers.")] = 4,
    per_host_rps: Annotated[
        float, typer.Option("--per-host-rps", help="Starting requests/sec per host.")
    ] = 4.0,
    per_host_concurrency: Annotated[
        int, typer.Option("--per-host-concurrency", min=1, help="Simultaneous fetches per host.")
    ] = 4,
    wait_until: Annotated[
        str,
        typer.Option("--wait-until", help="domcontentloaded | load | networkidle | commit."),
    ] = "domcontentloaded",
    wait_for: Annotated[
        str | None, typer.Option("--wait-for", help="CSS selector to wait for before capture.")
    ] = None,
    settle_ms: Annotated[
        int, typer.Option("--settle-ms", min=0, help="Extra wait after DOM ready, ms.")
    ] = 0,
    timeout_ms: Annotated[
        int, typer.Option("--timeout-ms", min=1000, help="Per-page navigation timeout, ms.")
    ] = 30_000,
    time_budget: Annotated[
        float, typer.Option("--time-budget", min=0, help="Whole-crawl seconds. 0 = unlimited.")
    ] = 0,
    retries: Annotated[int, typer.Option("--retries", min=0, help="Retries per URL.")] = 2,
    allow: Annotated[
        list[str] | None, typer.Option("--allow", help="Regex a URL must match (repeatable).")
    ] = None,
    deny: Annotated[
        list[str] | None, typer.Option("--deny", help="Regex that excludes a URL (repeatable).")
    ] = None,
    allow_domain: Annotated[
        list[str] | None, typer.Option("--allow-domain", help="Extra in-scope domain (repeatable).")
    ] = None,
    dedup: Annotated[
        str, typer.Option("--dedup", help="exact (precise) | bloom (fixed memory).")
    ] = "exact",
    no_render: Annotated[
        bool,
        typer.Option(
            "--no-render",
            help="Fetch HTML without a browser. Much faster; no JavaScript. (scrapy only)",
        ),
    ] = False,
    text_source: Annotated[
        str,
        typer.Option(
            "--text-source",
            help="engine (identical on every backend) | backend (native markdown where offered).",
        ),
    ] = "engine",
    include_html: Annotated[
        bool, typer.Option("--include-html", help="Keep raw HTML in the output.")
    ] = False,
    no_text: Annotated[bool, typer.Option("--no-text", help="Skip text extraction.")] = False,
    no_links: Annotated[bool, typer.Option("--no-links", help="Do not follow links.")] = False,
    no_robots: Annotated[
        bool, typer.Option("--no-robots", help="Ignore robots.txt (use only where authorised).")
    ] = False,
    headed: Annotated[bool, typer.Option("--headed", help="Show the browser window.")] = False,
    browser: Annotated[
        str, typer.Option("--browser", help="chromium | firefox | webkit.")
    ] = "chromium",
    proxy: Annotated[str | None, typer.Option("--proxy", help="Proxy URL.")] = None,
    stats_file: Annotated[
        Path | None, typer.Option("--stats", help="Write the final metrics JSON here.")
    ] = None,
    no_progress: Annotated[
        bool, typer.Option("--no-progress", help="Disable the live progress panel.")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Errors only.")] = False,
) -> None:
    """Crawl one or more seed URLs."""
    _setup_logging(verbose, quiet)

    if config_file is not None:
        cfg = CrawlConfig.from_file(config_file)
        if seeds:
            cfg.seeds = list(seeds)
        if backend != "crawl4ai":
            cfg.backend = backend
        if output is not None:
            cfg.output.path = output
    else:
        if not seeds:
            console.print("[red]error:[/] provide seed URLs or --config")
            raise typer.Exit(2)
        cfg = CrawlConfig(
            seeds=list(seeds),
            backend=backend,
            concurrency=concurrency,
            batch_size=batch_size,
            pipeline_depth=pipeline_depth,
            processor_workers=workers,
            max_pages=max_pages,
            max_depth=max_depth,
            max_retries=retries,
            time_budget_s=time_budget,
            scope=scope,  # type: ignore[arg-type]
            allow_patterns=list(allow or []),
            deny_patterns=list(deny or []),
            allow_domains=list(allow_domain or []),
            follow_links=not no_links,
            dedup=dedup,  # type: ignore[arg-type]
            proxy=proxy,
            verbose=verbose,
            progress=not no_progress,
            backend_options={"render": False} if no_render else {},
            browser={
                "headless": not headed,
                "browser_type": browser,
                "wait_until": wait_until,
                "wait_for_selector": wait_for,
                "settle_ms": settle_ms,
                "page_timeout_ms": timeout_ms,
            },
            politeness={
                "respect_robots": not no_robots,
                "per_host_rps": per_host_rps,
                "per_host_concurrency": per_host_concurrency,
            },
            output={
                "path": output,
                "format": "jsonl",
                "include_html": include_html,
                "include_text": not no_text,
                "include_links": not no_links,
                "text_source": text_source,
            },
        )

    loop_name = _install_uvloop()
    if verbose:
        console.print(f"[dim]event loop: {loop_name}[/]")

    show_progress = cfg.progress and not quiet and sys.stderr.isatty()
    try:
        metrics = asyncio.run(_run(cfg, show_progress))
    except KeyboardInterrupt:  # pragma: no cover - interactive path
        console.print("[yellow]interrupted[/]")
        raise typer.Exit(130) from None
    except Exception as exc:
        console.print(f"[red]crawl failed:[/] {exc}")
        if verbose:
            console.print_exception()
        raise typer.Exit(1) from exc

    if not quiet:
        _print_summary(metrics, cfg)
    if stats_file is not None:
        stats_file.parent.mkdir(parents=True, exist_ok=True)
        stats_file.write_text(json.dumps(metrics.summary(), indent=2), encoding="utf-8")

    # A crawl that fetched nothing successfully is a failure worth signalling.
    if metrics.succeeded == 0 and metrics.fetched > 0:
        raise typer.Exit(1)


async def _run(cfg: CrawlConfig, show_progress: bool) -> Metrics:
    engine = CrawlEngine(cfg)
    _install_signal_handlers(engine)

    if not show_progress:
        return await engine.run()

    run_task = asyncio.create_task(engine.run())
    with Live(_progress_table(engine), console=console, refresh_per_second=4) as live:
        while not run_task.done():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(run_task), timeout=0.25)
            live.update(_progress_table(engine))
        live.update(_progress_table(engine))
    return await run_task


def _install_signal_handlers(engine: CrawlEngine) -> None:
    """First Ctrl-C winds down cleanly; a second one aborts."""
    loop = asyncio.get_running_loop()
    state = {"hits": 0}

    def _handler() -> None:
        state["hits"] += 1
        if state["hits"] == 1:
            console.print(
                "[yellow]shutting down -- finishing in-flight pages (Ctrl-C again to abort)[/]"
            )
            engine.stop("SIGINT")
        else:
            raise KeyboardInterrupt

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, _handler)


def _progress_table(engine: CrawlEngine) -> Table:
    s: dict[str, Any] = engine.snapshot()
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold cyan", justify="right")
    table.add_column()

    done = int(s["fetched"])
    total = engine.config.max_pages
    pct = 100.0 * done / total if total else 0.0
    table.add_row("pages", f"{done}/{total}  [dim]({pct:.0f}%)[/]")
    table.add_row("rate", f"{s['recent_pps']:.1f} pages/s  [dim]avg {s['pages_per_second']:.1f}[/]")
    table.add_row(
        "ok / fail",
        f"[green]{s['succeeded']}[/] / [red]{s['failed']}[/]  [dim]retried {s['retried']}[/]",
    )
    table.add_row("in flight", str(s["in_flight"]))
    table.add_row(
        "frontier",
        f"{s['queued']} queued  [dim]{s['hosts']} hosts, {s['deferred_hosts']} cooling[/]",
    )
    table.add_row(
        "discovered",
        f"{s['discovered']}  [dim]{s['duplicates']} dup, {s['out_of_scope']} out-of-scope[/]",
    )
    lat = f"p50 {s['latency_p50_s']:.2f}s  p90 {s['latency_p90_s']:.2f}s"
    table.add_row("latency", lat)
    if s.get("paused_memory"):
        table.add_row("state", "[yellow]paused: memory pressure[/]")
    return table


def _print_summary(metrics: Metrics, cfg: CrawlConfig) -> None:
    s = metrics.summary()
    table = Table(title="crawl summary", title_style="bold", show_header=False, box=None)
    table.add_column(style="bold cyan", justify="right")
    table.add_column()
    table.add_row("backend", cfg.backend)
    table.add_row("elapsed", f"{s['elapsed_s']}s")
    table.add_row("fetched", f"{s['fetched']}  ({s['pages_per_second']} pages/s)")
    table.add_row("succeeded", f"[green]{s['succeeded']}[/]  ({s['success_rate'] * 100:.1f}%)")
    table.add_row("failed", f"[red]{s['failed']}[/]  (retried {s['retried']})")
    table.add_row("discovered", f"{s['discovered']} links, {s['enqueued']} enqueued")
    table.add_row(
        "skipped",
        f"{s['duplicates']} dup, {s['out_of_scope']} out-of-scope, {s['robots_blocked']} robots",
    )
    table.add_row(
        "latency",
        f"p50 {s['latency_p50_s']}s  p90 {s['latency_p90_s']}s  p99 {s['latency_p99_s']}s",
    )
    table.add_row("html", f"{s['mb_html']} MB")
    if s["status_counts"]:
        table.add_row("statuses", "  ".join(f"{k}:{v}" for k, v in s["status_counts"].items()))
    if s["errors"]:
        table.add_row("errors", "  ".join(f"{k}:{v}" for k, v in s["errors"].items()))
    console.print(table)


@app.command("backends")
def backends_cmd() -> None:
    """List discovered fetch backends and whether they are usable."""
    table = Table(title="fetch backends", title_style="bold")
    table.add_column("name", style="bold cyan")
    table.add_column("status")
    table.add_column("capabilities", style="dim")
    table.add_column("source", style="dim")
    table.add_column("target", style="dim")

    infos = list_backends()
    for info in infos:
        status = (
            "[green]available[/]"
            if info.available
            else f"[red]unavailable[/] [dim]{info.reason}[/]"
        )
        table.add_row(info.name, status, info.capabilities, info.source, info.target)
    console.print(table)
    if not any(i.available for i in infos):
        console.print("[yellow]no backends available -- try:[/] uv sync --extra all")


@app.command("bench")
def bench_cmd(
    url: Annotated[str, typer.Argument(help="Seed URL to benchmark against.")],
    backends: Annotated[
        str, typer.Option("--backends", help="Comma-separated backends to compare.")
    ] = "crawl4ai,crawlee,scrapy",
    max_pages: Annotated[
        int, typer.Option("--max-pages", "-n", min=1, help="Pages to fetch per run.")
    ] = 60,
    concurrency: Annotated[
        str, typer.Option("--concurrency", "-j", help="Comma-separated concurrency levels.")
    ] = "8,16,32",
    repeat: Annotated[int, typer.Option("--repeat", min=1, help="Runs per combination.")] = 1,
    no_render: Annotated[
        bool, typer.Option("--no-render", help="Also time scrapy without a browser.")
    ] = False,
    max_depth: Annotated[int, typer.Option("--max-depth", min=0)] = 3,
    scope: Annotated[str, typer.Option("--scope")] = "host",
    no_robots: Annotated[bool, typer.Option("--no-robots")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Measure pages/s per backend and concurrency level.

    Output is discarded so the numbers reflect fetching and parsing rather than
    disk. Runs are sequential -- overlapping them would have the backends
    competing for the same CPU and the same target host.
    """
    _setup_logging(verbose, quiet=not verbose)

    names = [n.strip() for n in backends.split(",") if n.strip()]
    levels = [int(x) for x in concurrency.split(",") if x.strip()]
    if no_render and "scrapy" in names:
        names.append("scrapy:no-render")

    _install_uvloop()
    rows: list[dict[str, Any]] = []

    for name in names:
        backend, _, variant = name.partition(":")
        for level in levels:
            for _ in range(repeat):
                cfg = CrawlConfig(
                    seeds=[url],
                    backend=backend,
                    concurrency=level,
                    batch_size=max(1, min(level, 32)),
                    max_pages=max_pages,
                    max_depth=max_depth,
                    scope=scope,  # type: ignore[arg-type]
                    progress=False,
                    verbose=verbose,
                    backend_options={"render": False} if variant == "no-render" else {},
                    politeness={
                        "respect_robots": not no_robots,
                        # Politeness would otherwise be what is measured.
                        "per_host_rps": float(level * 4),
                        "per_host_concurrency": level,
                    },
                    output={"format": "none"},
                )
                console.print(f"[dim]running {name} at concurrency {level}...[/]")
                try:
                    metrics = asyncio.run(CrawlEngine(cfg).run())
                except Exception as exc:
                    console.print(f"[red]{name} @ {level} failed:[/] {exc}")
                    continue
                s = metrics.summary()
                rows.append(
                    {
                        "backend": name,
                        "concurrency": level,
                        "pages": s["fetched"],
                        "pps": s["pages_per_second"],
                        "elapsed": s["elapsed_s"],
                        "p50": s["latency_p50_s"],
                        "p90": s["latency_p90_s"],
                        "ok": s["success_rate"],
                    }
                )

    if not rows:
        console.print("[red]no successful runs[/]")
        raise typer.Exit(1)

    table = Table(title=f"throughput -- {url}", title_style="bold")
    table.add_column("backend", style="bold cyan")
    table.add_column("conc", justify="right")
    table.add_column("pages", justify="right")
    table.add_column("pages/s", justify="right", style="bold")
    table.add_column("elapsed", justify="right", style="dim")
    table.add_column("p50", justify="right", style="dim")
    table.add_column("p90", justify="right", style="dim")
    table.add_column("ok", justify="right", style="dim")

    best = max(r["pps"] for r in rows)
    for r in rows:
        pps = f"{r['pps']:.2f}"
        table.add_row(
            r["backend"],
            str(r["concurrency"]),
            str(r["pages"]),
            f"[green]{pps}[/]" if r["pps"] == best else pps,
            f"{r['elapsed']}s",
            f"{r['p50']}s",
            f"{r['p90']}s",
            f"{r['ok'] * 100:.0f}%",
        )
    console.print(table)


@app.command("config")
def config_cmd(
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write the template here.")
    ] = None,
) -> None:
    """Print a fully-populated config template with every default filled in."""
    cfg = CrawlConfig(seeds=["https://example.com"])
    text = cfg.to_yaml()
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        console.print(f"wrote {output}")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    app()
