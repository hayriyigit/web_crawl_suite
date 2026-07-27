"""HTML -> links, title, text.

Runs once per fetched page and is the only CPU-bound step in the pipeline, so
it uses selectolax (the lexbor C parser) rather than BeautifulSoup/lxml --
roughly an order of magnitude faster on real-world pages, which matters when
the fetch side is delivering hundreds of pages a second.

Doing extraction centrally rather than per-backend is what makes backends
interchangeable: every backend produces the same fields regardless of what its
underlying library does or does not offer.
"""

from __future__ import annotations

from selectolax.lexbor import LexborHTMLParser

from .urls import join, normalize

__all__ = ["PageExtract", "extract_links", "extract_page"]

# Elements whose text is never page content. Deliberately limited to markup
# that is not prose: boilerplate containers like <nav>/<header>/<footer> are
# kept, because on plenty of sites they hold real content and a general-purpose
# crawler should not silently discard it.
_STRIP_TAGS = ["script", "style", "noscript", "template", "svg", "canvas", "iframe"]


class PageExtract:
    """Result of parsing one HTML document."""

    __slots__ = ("canonical", "links", "meta_description", "nofollow", "noindex", "text", "title")

    def __init__(
        self,
        title: str = "",
        text: str = "",
        links: list[str] | None = None,
        canonical: str | None = None,
        meta_description: str = "",
        noindex: bool = False,
        nofollow: bool = False,
    ) -> None:
        self.title = title
        self.text = text
        self.links = links or []
        self.canonical = canonical
        self.meta_description = meta_description
        self.noindex = noindex
        self.nofollow = nofollow


def _parse(html: str) -> LexborHTMLParser | None:
    if not html:
        return None
    try:
        return LexborHTMLParser(html)
    except Exception:
        return None


def extract_links(html: str, base_url: str, *, strip_query: bool = False) -> list[str]:
    """Absolute, normalised, de-duplicated outbound links."""
    tree = _parse(html)
    if tree is None:
        return []
    return _links_from_tree(tree, base_url, strip_query)


def _links_from_tree(tree: LexborHTMLParser, base_url: str, strip_query: bool) -> list[str]:
    # An explicit <base href> changes how every relative link resolves.
    base_node = tree.css_first("base[href]")
    if base_node is not None:
        href = (base_node.attributes.get("href") or "").strip()
        if href:
            base_url = join(base_url, href) or base_url

    seen: set[str] = set()
    out: list[str] = []
    for node in tree.css("a[href]"):
        attrs = node.attributes
        href = attrs.get("href")
        if not href:
            continue
        href = href.strip()
        if not href or href[0] == "#":
            continue
        rel = attrs.get("rel")
        if rel and "nofollow" in rel.lower():
            continue
        absolute = join(base_url, href)
        if not absolute:
            continue
        norm = normalize(absolute, strip_query=strip_query)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def extract_page(
    html: str,
    base_url: str,
    *,
    want_links: bool = True,
    want_text: bool = True,
    strip_query: bool = False,
    max_text_chars: int = 0,
) -> PageExtract:
    """Parse a document once and pull out everything the pipeline needs."""
    tree = _parse(html)
    if tree is None:
        return PageExtract()

    result = PageExtract()

    title_node = tree.css_first("title")
    if title_node is not None:
        result.title = (title_node.text() or "").strip()[:512]

    for meta in tree.css("meta"):
        attrs = meta.attributes
        name = (attrs.get("name") or "").lower()
        if name == "description":
            result.meta_description = (attrs.get("content") or "").strip()[:1024]
        elif name == "robots":
            content = (attrs.get("content") or "").lower()
            result.noindex = "noindex" in content
            result.nofollow = "nofollow" in content

    canonical = tree.css_first('link[rel="canonical"][href]')
    if canonical is not None:
        href = (canonical.attributes.get("href") or "").strip()
        if href:
            result.canonical = normalize(join(base_url, href), strip_query=strip_query) or None

    if want_links and not result.nofollow:
        result.links = _links_from_tree(tree, base_url, strip_query)

    if want_text:
        # strip_tags drops the elements *and* their contents (unwrap_tags would
        # keep the script bodies as text). Safe to mutate here: links were
        # already collected above.
        try:
            tree.strip_tags(_STRIP_TAGS)
        except Exception:
            pass
        body = tree.body or tree.root
        if body is not None:
            text = body.text(separator=" ", strip=True) or ""
            text = " ".join(text.split())
            if max_text_chars and len(text) > max_text_chars:
                text = text[:max_text_chars]
            result.text = text

    return result
