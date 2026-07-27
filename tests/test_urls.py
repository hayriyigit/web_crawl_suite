from __future__ import annotations

import pytest

from polycrawl.urls import (
    ScopeRules,
    has_binary_extension,
    normalize,
    registrable_domain,
    same_site,
)


class TestNormalize:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://Example.COM/Path", "https://example.com/Path"),
            ("https://example.com", "https://example.com/"),
            ("https://example.com:443/a", "https://example.com/a"),
            ("http://example.com:80/a", "http://example.com/a"),
            ("https://example.com:8443/a", "https://example.com:8443/a"),
            ("https://example.com/a#frag", "https://example.com/a"),
            ("https://example.com/a//b///c", "https://example.com/a/b/c"),
        ],
    )
    def test_canonical_forms(self, raw: str, expected: str) -> None:
        assert normalize(raw) == expected

    def test_strips_tracking_params_but_keeps_real_ones(self) -> None:
        url = "https://example.com/p?utm_source=x&id=7&fbclid=abc&page=2"
        assert normalize(url) == "https://example.com/p?id=7&page=2"

    def test_sorts_query_so_orderings_collapse(self) -> None:
        a = normalize("https://example.com/p?b=2&a=1")
        b = normalize("https://example.com/p?a=1&b=2")
        assert a == b

    def test_strip_query_option(self) -> None:
        assert normalize("https://example.com/p?a=1", strip_query=True) == "https://example.com/p"

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "#anchor",
            "javascript:void(0)",
            "mailto:a@b.com",
            "tel:+1234",
            "data:text/html,<p>x",
            "ftp://example.com/f",
            "not a url",
        ],
    )
    def test_rejects_uncrawlable(self, raw: str) -> None:
        assert normalize(raw) == ""

    def test_keeps_fragment_when_asked(self) -> None:
        assert normalize("https://example.com/a#f", keep_fragment=True).endswith("#f")


class TestDomains:
    @pytest.mark.parametrize(
        ("host", "expected"),
        [
            ("example.com", "example.com"),
            ("www.example.com", "example.com"),
            ("a.b.example.com", "example.com"),
            ("example.co.uk", "example.co.uk"),
            ("shop.example.co.uk", "example.co.uk"),
            ("127.0.0.1", "127.0.0.1"),
        ],
    )
    def test_registrable_domain(self, host: str, expected: str) -> None:
        assert registrable_domain(host) == expected

    def test_same_site(self) -> None:
        assert same_site("https://a.example.com/x", "https://b.example.com/y")
        assert not same_site("https://example.com", "https://other.com")


class TestBinaryDetection:
    @pytest.mark.parametrize(
        "url", ["https://e.com/f.pdf", "https://e.com/a/b.JPG", "https://e.com/x.zip"]
    )
    def test_detects_binary(self, url: str) -> None:
        assert has_binary_extension(url)

    @pytest.mark.parametrize(
        "url", ["https://e.com/page", "https://e.com/a.html", "https://e.com/x.php?a=1"]
    )
    def test_allows_html(self, url: str) -> None:
        assert not has_binary_extension(url)


class TestScopeRules:
    def test_site_scope_spans_subdomains(self) -> None:
        rules = ScopeRules(["https://example.com/"], mode="site", max_depth=5)
        assert rules.allows("https://blog.example.com/a", 1)
        assert not rules.allows("https://other.com/a", 1)

    def test_host_scope_is_exact(self) -> None:
        rules = ScopeRules(["https://example.com/"], mode="host", max_depth=5)
        assert rules.allows("https://example.com/a", 1)
        assert not rules.allows("https://blog.example.com/a", 1)

    def test_path_scope_restricts_to_seed_prefix(self) -> None:
        rules = ScopeRules(["https://example.com/docs/intro"], mode="path", max_depth=5)
        assert rules.allows("https://example.com/docs/other", 1)
        assert not rules.allows("https://example.com/blog/x", 1)

    def test_any_scope_allows_offsite(self) -> None:
        rules = ScopeRules(["https://example.com/"], mode="any", max_depth=5)
        assert rules.allows("https://other.com/a", 1)

    def test_depth_limit(self) -> None:
        rules = ScopeRules(["https://example.com/"], max_depth=2)
        assert rules.allows("https://example.com/a", 2)
        assert not rules.allows("https://example.com/a", 3)

    def test_deny_beats_allow(self) -> None:
        rules = ScopeRules(
            ["https://example.com/"],
            allow_patterns=[r"/docs/"],
            deny_patterns=[r"/docs/private"],
            max_depth=5,
        )
        assert rules.allows("https://example.com/docs/a", 1)
        assert not rules.allows("https://example.com/docs/private/a", 1)
        assert not rules.allows("https://example.com/blog/a", 1)

    def test_allow_domain_overrides_scope_mode(self) -> None:
        rules = ScopeRules(
            ["https://example.com/"], mode="site", allow_domains=["cdn.other.com"], max_depth=5
        )
        assert rules.allows("https://cdn.other.com/a", 1)
        assert not rules.allows("https://evil.com/a", 1)

    def test_deny_domain(self) -> None:
        rules = ScopeRules(
            ["https://example.com/"], mode="any", deny_domains=["ads.example.com"], max_depth=5
        )
        assert not rules.allows("https://ads.example.com/x", 1)

    def test_binary_skipped(self) -> None:
        rules = ScopeRules(["https://example.com/"], max_depth=5, skip_binary=True)
        assert not rules.allows("https://example.com/f.pdf", 1)
