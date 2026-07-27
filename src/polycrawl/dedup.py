"""URL dedup structures.

The "seen" set is touched once per discovered link, and on a large crawl it is
the single biggest resident allocation in the process. Two implementations are
offered so the memory/accuracy trade-off is an explicit choice:

* :class:`HashSetSeen` -- exact, ~60-70 bytes per URL (a Python ``set`` of
  64-bit ints). Fine up to a few million URLs.
* :class:`BloomSeen`   -- approximate, a fixed byte budget chosen up front,
  with a tunable false-positive rate. A false positive silently skips a URL,
  which is an acceptable loss on a 100M-page crawl where storing an exact set
  would need tens of gigabytes.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from hashlib import blake2b

__all__ = ["BloomSeen", "HashSetSeen", "SeenSet", "make_seen_set"]


class SeenSet(ABC):
    """Membership filter over normalised URLs."""

    __slots__ = ()

    @abstractmethod
    def add(self, url: str) -> bool:
        """Add ``url``; return ``True`` if it was newly added (i.e. not seen)."""

    @abstractmethod
    def __contains__(self, url: str) -> bool: ...

    @abstractmethod
    def __len__(self) -> int: ...


class HashSetSeen(SeenSet):
    """Exact membership via a set of 64-bit digests.

    Storing digests rather than the strings themselves cuts memory roughly 3-5x
    on typical URLs and makes the hash cost independent of URL length.
    """

    __slots__ = ("_seen",)

    def __init__(self) -> None:
        self._seen: set[int] = set()

    def add(self, url: str) -> bool:
        # ``hash()`` on str is SipHash: fast, and process-local randomisation is
        # irrelevant because the set never outlives the process.
        h = hash(url)
        n = len(self._seen)
        self._seen.add(h)
        return len(self._seen) != n

    def __contains__(self, url: str) -> bool:
        return hash(url) in self._seen

    def __len__(self) -> int:
        return len(self._seen)


class BloomSeen(SeenSet):
    """Fixed-memory approximate membership filter.

    Sized from an expected item count and target false-positive rate. One
    ``blake2b`` call per URL is sliced into the required number of independent
    indices, so cost does not grow with ``k``.
    """

    __slots__ = ("_bits", "_capacity", "_count", "_k", "_size")

    def __init__(self, capacity: int = 10_000_000, error_rate: float = 0.001) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if not 0.0 < error_rate < 1.0:
            raise ValueError("error_rate must be in (0, 1)")
        size = int(-capacity * math.log(error_rate) / (math.log(2) ** 2))
        self._size = max(8, size)
        # blake2b gives us 64 bytes; each index consumes 8, capping k at 8.
        self._k = max(1, min(8, round(self._size / capacity * math.log(2))))
        self._bits = bytearray((self._size + 7) // 8)
        self._count = 0
        self._capacity = capacity

    @property
    def nbytes(self) -> int:
        return len(self._bits)

    def _indices(self, url: str) -> list[int]:
        digest = blake2b(url.encode("utf-8", "ignore"), digest_size=8 * self._k).digest()
        size = self._size
        return [int.from_bytes(digest[i * 8 : i * 8 + 8], "little") % size for i in range(self._k)]

    def add(self, url: str) -> bool:
        bits = self._bits
        new = False
        for idx in self._indices(url):
            byte, mask = idx >> 3, 1 << (idx & 7)
            if not bits[byte] & mask:
                bits[byte] |= mask
                new = True
        if new:
            self._count += 1
        return new

    def __contains__(self, url: str) -> bool:
        bits = self._bits
        return all(bits[i >> 3] & (1 << (i & 7)) for i in self._indices(url))

    def __len__(self) -> int:
        return self._count

    @property
    def saturation(self) -> float:
        """Fraction of the designed capacity consumed; >1.0 means degraded FP rate."""
        return self._count / self._capacity


def make_seen_set(
    kind: str = "exact", *, capacity: int = 10_000_000, error_rate: float = 0.001
) -> SeenSet:
    if kind == "exact":
        return HashSetSeen()
    if kind == "bloom":
        return BloomSeen(capacity=capacity, error_rate=error_rate)
    raise ValueError(f"unknown dedup kind: {kind!r} (expected 'exact' or 'bloom')")
