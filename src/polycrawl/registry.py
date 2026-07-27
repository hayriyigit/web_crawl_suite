"""Backend discovery.

Backends are resolved lazily by name. Nothing is imported until it is actually
requested, so having ``crawl4ai`` installed but not ``crawlee`` (or neither)
never breaks start-up -- an unavailable backend simply reports why.

Three registration routes, checked in this order:

1. Built-ins, wired below.
2. ``polycrawl.backends`` entry points from any installed distribution.
3. Runtime registration via :func:`register`, for in-process plugins.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from importlib import import_module
from importlib.metadata import entry_points
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backend import CrawlerBackend

__all__ = ["BackendInfo", "get_backend", "list_backends", "register"]

#: Shipped backends: name -> "module:attr". Kept as strings so importing the
#: registry never pulls in playwright, litellm and friends.
_BUILTINS: dict[str, str] = {
    "crawl4ai": "polycrawl.backends.crawl4ai_backend:Crawl4AIBackend",
    "crawlee": "polycrawl.backends.crawlee_backend:CrawleeBackend",
    "scrapy": "polycrawl.backends.scrapy_backend:ScrapyBackend",
}

_runtime: dict[str, type[CrawlerBackend]] = {}
_entry_point_cache: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class BackendInfo:
    """A discovered backend, without necessarily having imported it."""

    name: str
    target: str
    available: bool
    reason: str
    capabilities: str
    source: str


def register(backend_cls: type[CrawlerBackend], name: str | None = None) -> type[CrawlerBackend]:
    """Register a backend class at runtime.

    Usable as a decorator::

        @register
        class MyBackend(CrawlerBackend):
            name = "mine"
    """
    key = name or getattr(backend_cls, "name", "") or backend_cls.__name__.lower()
    if not key:
        raise ValueError("backend must define a non-empty 'name'")
    _runtime[key] = backend_cls
    return backend_cls


def _discover_entry_points() -> dict[str, str]:
    global _entry_point_cache
    if _entry_point_cache is None:
        found: dict[str, str] = {}
        try:
            for ep in entry_points(group="polycrawl.backends"):
                found[ep.name] = ep.value
        except Exception:  # pragma: no cover - metadata backends vary
            pass
        _entry_point_cache = found
    return _entry_point_cache


def _load(target: str) -> type[CrawlerBackend]:
    module_name, _, attr = target.partition(":")
    if not attr:
        raise ValueError(f"malformed backend target {target!r}, expected 'module:Class'")
    return getattr(import_module(module_name), attr)


def _targets() -> dict[str, tuple[str, str]]:
    """All known names -> (target, source), later sources winning."""
    out: dict[str, tuple[str, str]] = {}
    for name, target in _BUILTINS.items():
        out[name] = (target, "builtin")
    for name, target in _discover_entry_points().items():
        # Entry points that merely re-advertise a builtin keep the builtin label.
        source = "builtin" if out.get(name, ("", ""))[0] == target else "entry-point"
        out[name] = (target, source)
    for name, cls in _runtime.items():
        out[name] = (f"{cls.__module__}:{cls.__qualname__}", "runtime")
    return out


def get_backend(name: str) -> type[CrawlerBackend]:
    """Import and return the backend class registered under ``name``."""
    if name in _runtime:
        return _runtime[name]
    targets = _targets()
    if name not in targets:
        known = ", ".join(sorted(targets)) or "<none>"
        raise KeyError(f"unknown backend {name!r}. Available: {known}")
    target, _ = targets[name]
    try:
        return _load(target)
    except ImportError as exc:
        raise RuntimeError(
            f"backend {name!r} is registered at {target!r} but could not be imported: {exc}"
        ) from exc


def iter_backends() -> Iterator[BackendInfo]:
    """Yield every known backend with its availability, importing defensively."""
    for name, (target, source) in sorted(_targets().items()):
        try:
            cls = _load(target)
        except Exception as exc:
            yield BackendInfo(name, target, False, f"import failed: {exc}", "-", source)
            continue
        try:
            available, reason = cls.is_available()
        except Exception as exc:  # pragma: no cover - defensive
            available, reason = False, str(exc)
        yield BackendInfo(
            name=name,
            target=target,
            available=available,
            reason=reason,
            capabilities=cls.capabilities.describe(),
            source=source,
        )


def list_backends() -> list[BackendInfo]:
    return list(iter_backends())
