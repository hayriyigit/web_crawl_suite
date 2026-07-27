"""System memory pressure probe.

Headless browsers keep most of their memory in child processes, so measuring
this process's RSS understates real usage badly. What actually matters is
whether the *machine* is close to swapping or to the OOM killer, so this reads
system-wide availability instead.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["memory_probe_available", "memory_used_pct"]

_MEMINFO = Path("/proc/meminfo")


def _from_proc() -> float | None:
    """Used-memory percentage from ``MemAvailable``, the kernel's own estimate."""
    try:
        total = available = None
        with _MEMINFO.open("rb") as fh:
            for raw in fh:
                if raw.startswith(b"MemTotal:"):
                    total = int(raw.split()[1])
                elif raw.startswith(b"MemAvailable:"):
                    available = int(raw.split()[1])
                if total is not None and available is not None:
                    break
        if not total:
            return None
        return 100.0 * (1.0 - (available or 0) / total)
    except (OSError, ValueError, IndexError):
        return None


def _from_psutil() -> float | None:
    try:
        import psutil
    except ImportError:
        return None
    try:
        return float(psutil.virtual_memory().percent)
    except Exception:
        return None


def memory_used_pct() -> float | None:
    """System memory in use, 0-100, or ``None`` if it cannot be determined."""
    return _from_proc() or _from_psutil()


def memory_probe_available() -> bool:
    return memory_used_pct() is not None
