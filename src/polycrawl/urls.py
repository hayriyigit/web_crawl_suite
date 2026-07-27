"""URL normalisation and scope rules.

Normalisation runs once per discovered link -- on a heavy crawl that is tens of
millions of calls -- so the hot paths avoid regex and keep allocations down.
"""

from __future__ import annotations

import re
from functools import lru_cache
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

__all__ = [
    "ScopeRules",
    "host_of",
    "is_crawlable_scheme",
    "join",
    "normalize",
    "registrable_domain",
    "same_site",
]

_DEFAULT_PORTS = {"http": "80", "https": "443"}
_CRAWLABLE_SCHEMES = frozenset({"http", "https"})

# Tracking parameters carry no content and only fragment the URL space, which
# wastes fetches and inflates the dedup set. Dropped during normalisation.
_JUNK_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "utm_source_platform",
        "utm_creative_format",
        "gclid",
        "gclsrc",
        "dclid",
        "fbclid",
        "msclkid",
        "twclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "_ga",
        "_gl",
        "yclid",
        "ref_src",
        "ref_url",
        "s_kwcid",
        "sc_campaign",
        "sc_channel",
        "vero_id",
        "wickedid",
    }
)

# Extensions that are never HTML. Filtering here avoids paying for a full
# browser navigation just to discover the response is a binary.
_BINARY_EXT = frozenset(
    """.jpg .jpeg .png .gif .webp .avif .svg .ico .bmp .tiff .heic
    .mp4 .webm .mkv .avi .mov .wmv .flv .m4v .mpg .mpeg .ogv
    .mp3 .wav .flac .aac .ogg .oga .m4a .opus .wma
    .pdf .doc .docx .xls .xlsx .ppt .pptx .odt .ods .odp .rtf
    .zip .gz .tgz .bz2 .xz .7z .rar .tar .iso .dmg .pkg .deb .rpm .msi .exe .apk
    .css .js .mjs .map .woff .woff2 .ttf .otf .eot
    .json .xml .rss .atom .csv .tsv .txt.gz
    .wasm .bin .dat .swf .psd .ai .eps""".split()
)


@lru_cache(maxsize=100_000)
def host_of(url: str) -> str:
    """Lower-cased hostname without port, or ``""`` when unparseable."""
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


@lru_cache(maxsize=50_000)
def registrable_domain(host: str) -> str:
    """Approximate eTLD+1.

    Uses a heuristic instead of the full public-suffix list: the PSL costs a
    dependency plus a lookup per link, and the failure mode here (treating
    ``a.co.uk`` and ``b.co.uk`` as one site) only matters for the ``site``
    scope, where an explicit ``--allow-domain`` is the precise tool anyway.
    """
    if not host or host.replace(".", "").isdigit():
        return host
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    # Two-part public suffixes we care about most often.
    if parts[-2] in {"co", "com", "org", "net", "gov", "edu", "ac"} and len(parts[-1]) <= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def same_site(a: str, b: str) -> bool:
    return registrable_domain(host_of(a)) == registrable_domain(host_of(b))


def is_crawlable_scheme(url: str) -> bool:
    scheme, _, rest = url.partition(":")
    return scheme.lower() in _CRAWLABLE_SCHEMES and rest.startswith("//")


def join(base: str, link: str) -> str:
    """Resolve ``link`` against ``base``, tolerating malformed hrefs."""
    try:
        return urljoin(base, link)
    except ValueError:
        return ""


def normalize(url: str, *, strip_query: bool = False, keep_fragment: bool = False) -> str:
    """Canonicalise a URL so equivalent forms collapse to one dedup key.

    Applies: scheme/host lower-casing, default-port removal, fragment removal,
    tracking-parameter stripping, query-parameter sorting and empty-path
    filling. Returns ``""`` for anything not worth crawling.
    """
    if not url:
        return ""
    url = url.strip()
    if not url or url[0] in "#":
        return ""

    low = url[:11].lower()
    if low.startswith(("javascript:", "mailto:", "tel:", "data:", "about:", "blob:", "ftp:")):
        return ""

    if not keep_fragment:
        url, _ = urldefrag(url)

    try:
        parts = urlsplit(url)
    except ValueError:
        return ""

    scheme = parts.scheme.lower()
    if scheme not in _CRAWLABLE_SCHEMES:
        return ""

    host = (parts.hostname or "").lower()
    if not host:
        return ""
    try:
        host = host.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        pass

    netloc = host
    port = parts.port
    if port is not None and str(port) != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{port}"

    path = parts.path or "/"
    # Collapse duplicate slashes, which are semantically identical on nearly
    # every server but produce distinct dedup keys.
    if "//" in path:
        path = re.sub(r"/{2,}", "/", path)

    query = ""
    if parts.query and not strip_query:
        kept = []
        for pair in parts.query.split("&"):
            if not pair:
                continue
            key = pair.split("=", 1)[0]
            if key.lower() in _JUNK_PARAMS:
                continue
            kept.append(pair)
        # Sorting makes ``?a=1&b=2`` and ``?b=2&a=1`` a single key.
        query = "&".join(sorted(kept))

    return urlunsplit((scheme, netloc, path, query, parts.fragment if keep_fragment else ""))


def has_binary_extension(url: str) -> bool:
    path = urlsplit(url).path
    dot = path.rfind(".")
    if dot == -1 or dot < path.rfind("/"):
        return False
    return path[dot:].lower() in _BINARY_EXT


class ScopeRules:
    """Decides whether a discovered URL is in scope for the crawl.

    Compiled once at start-up; ``allows()`` is called for every discovered link.
    """

    __slots__ = (
        "_seed_domains",
        "_seed_prefixes",
        "allow_domains",
        "allow_re",
        "deny_domains",
        "deny_re",
        "max_depth",
        "mode",
        "seeds",
        "skip_binary",
    )

    def __init__(
        self,
        seeds: list[str],
        *,
        mode: str = "site",
        allow_patterns: list[str] | None = None,
        deny_patterns: list[str] | None = None,
        allow_domains: list[str] | None = None,
        deny_domains: list[str] | None = None,
        max_depth: int = 2,
        skip_binary: bool = True,
    ) -> None:
        self.mode = mode
        self.seeds = seeds
        self.max_depth = max_depth
        self.skip_binary = skip_binary
        self._seed_domains = {registrable_domain(host_of(s)) for s in seeds}
        self._seed_prefixes = tuple(s.rsplit("/", 1)[0] if "/" in s[8:] else s for s in seeds)
        self.allow_re = (
            re.compile("|".join(f"(?:{p})" for p in allow_patterns)) if allow_patterns else None
        )
        self.deny_re = (
            re.compile("|".join(f"(?:{p})" for p in deny_patterns)) if deny_patterns else None
        )
        self.allow_domains = {d.lower().lstrip(".") for d in (allow_domains or [])}
        self.deny_domains = {d.lower().lstrip(".") for d in (deny_domains or [])}

    def allows(self, url: str, depth: int) -> bool:
        if depth > self.max_depth:
            return False
        if self.skip_binary and has_binary_extension(url):
            return False

        host = host_of(url)
        if not host:
            return False

        if self.deny_domains and self._domain_matches(host, self.deny_domains):
            return False
        if self.deny_re is not None and self.deny_re.search(url):
            return False

        # An explicit allow-domain overrides the scope mode entirely.
        if self.allow_domains and self._domain_matches(host, self.allow_domains):
            return self.allow_re is None or bool(self.allow_re.search(url))

        if self.mode == "site":
            if registrable_domain(host) not in self._seed_domains:
                return False
        elif self.mode == "host":
            if host not in {host_of(s) for s in self.seeds}:
                return False
        elif self.mode == "path":
            if not url.startswith(self._seed_prefixes):
                return False
        # mode == "any": no host restriction.

        if self.allow_re is not None and not self.allow_re.search(url):
            return False
        return True

    @staticmethod
    def _domain_matches(host: str, domains: set[str]) -> bool:
        if host in domains:
            return True
        return any(host.endswith("." + d) for d in domains)
