"""Bundled fetch backends.

Nothing is imported here on purpose. Each backend pulls in a heavy optional
dependency (playwright, litellm, ...), so they are loaded lazily by
:mod:`polycrawl.registry` only when actually selected.
"""

from __future__ import annotations

__all__: list[str] = []
