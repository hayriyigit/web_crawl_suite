"""Unit tests for the request-blocking policy and the resource trace.

No browser here: these pin the decisions -- what counts as a prefetch, which
hosts match, and above all whether a route is needed at all. That last one is
load-bearing, because installing a route costs the browser's HTTP cache, so a
policy that reports ``needed`` when nothing is configured would make every crawl
1.63x slower for no reason.
"""

from __future__ import annotations

from polycrawl.config import BrowserSettings
from polycrawl.resources import BlockPolicy, ResourceTrace, is_prefetch


class TestIsPrefetch:
    def test_purpose_header(self) -> None:
        assert is_prefetch({"purpose": "prefetch"})

    def test_sec_purpose_header(self) -> None:
        assert is_prefetch({"sec-purpose": "prefetch;anonymous-client-ip"})

    def test_header_case_does_not_matter(self) -> None:
        assert is_prefetch({"Purpose": "Prefetch"})

    def test_ordinary_request_is_not_prefetch(self) -> None:
        assert not is_prefetch({"accept": "text/html", "purpose": ""})

    def test_no_headers(self) -> None:
        assert not is_prefetch({})


class TestBlockPolicyNeeded:
    def test_default_settings_need_no_route(self) -> None:
        # The important one: blocking images and fonts happens through launch
        # flags, so an ordinary crawl must not end up with a route installed.
        policy = BlockPolicy.from_settings(BrowserSettings())
        assert not policy.needed

    def test_prefetch_needs_a_route(self) -> None:
        policy = BlockPolicy.from_settings(BrowserSettings(block_prefetch=True))
        assert policy.needed

    def test_hosts_need_a_route(self) -> None:
        policy = BlockPolicy.from_settings(BrowserSettings(blocked_hosts=["example.com"]))
        assert policy.needed

    def test_leftover_resource_types_need_a_route(self) -> None:
        policy = BlockPolicy.from_settings(
            BrowserSettings(), leftover_types=frozenset({"stylesheet"})
        )
        assert policy.needed

    def test_blank_host_entries_are_ignored(self) -> None:
        policy = BlockPolicy.from_settings(BrowserSettings(blocked_hosts=["", "  "]))
        assert not policy.needed


class TestHostMatching:
    def _policy(self, *hosts: str) -> BlockPolicy:
        return BlockPolicy.from_settings(BrowserSettings(blocked_hosts=list(hosts)))

    def test_exact_host(self) -> None:
        assert self._policy("google-analytics.com").blocks_host("https://google-analytics.com/c")

    def test_subdomain(self) -> None:
        policy = self._policy("google-analytics.com")
        assert policy.blocks_host("https://www.google-analytics.com/collect")

    def test_suffix_alone_does_not_match(self) -> None:
        # `notgoogle-analytics.com` ends with the denied string but is a
        # different registrable domain, so a naive endswith would be wrong.
        policy = self._policy("google-analytics.com")
        assert not policy.blocks_host("https://notgoogle-analytics.com/x")

    def test_unrelated_host(self) -> None:
        assert not self._policy("example.com").blocks_host("https://other.test/a")

    def test_port_is_ignored(self) -> None:
        assert self._policy("localhost").blocks_host("http://localhost:8123/beacon")

    def test_credentials_are_ignored(self) -> None:
        assert self._policy("example.com").blocks_host("https://user:pw@example.com/x")

    def test_case_is_ignored(self) -> None:
        assert self._policy("Example.COM").blocks_host("https://EXAMPLE.com/x")

    def test_leading_dot_is_accepted(self) -> None:
        assert self._policy(".example.com").blocks_host("https://a.example.com/x")


class TestBlockReason:
    def test_names_the_rule_that_fired(self) -> None:
        policy = BlockPolicy(prefetch=True, hosts=frozenset({"ads.test"}))
        assert policy.block_reason("https://x.test/a", "document", {"purpose": "prefetch"}) == (
            "prefetch"
        )
        assert policy.block_reason("https://ads.test/a", "script", {}) == "host"
        assert policy.block_reason("https://x.test/a", "script", {}) is None

    def test_resource_type_reason(self) -> None:
        policy = BlockPolicy(resource_types=frozenset({"stylesheet"}))
        assert policy.block_reason("https://x.test/a.css", "stylesheet", {}) == "stylesheet"

    def test_prefetch_not_blocked_when_disabled(self) -> None:
        policy = BlockPolicy(prefetch=False)
        assert policy.block_reason("https://x.test/a", "document", {"purpose": "prefetch"}) is None

    def test_should_block_agrees_with_reason(self) -> None:
        policy = BlockPolicy(prefetch=True)
        headers = {"purpose": "prefetch"}
        assert policy.should_block("https://x.test/a", "document", headers)
        assert not policy.should_block("https://x.test/a", "document", {})


class TestResourceTrace:
    def test_counts_requests_and_bytes(self) -> None:
        trace = ResourceTrace()
        trace.record_page()
        trace.record_request("script")
        trace.record_request("script")
        trace.record_bytes("script", 1_000)
        assert trace.total_requests == 2
        assert trace.total_bytes == 1_000

    def test_zero_length_responses_are_not_counted_as_bytes(self) -> None:
        trace = ResourceTrace()
        trace.record_bytes("document", 0)
        assert trace.total_bytes == 0

    def test_cache_hit_rate_is_none_until_something_resolves(self) -> None:
        assert ResourceTrace().cache_hit_rate is None

    def test_cache_hit_rate(self) -> None:
        trace = ResourceTrace()
        for _ in range(3):
            trace.record_cache(hit=True)
        trace.record_cache(hit=False)
        assert trace.cache_hit_rate == 0.75

    def test_summary_reports_blocked_and_cache(self) -> None:
        trace = ResourceTrace()
        trace.record_page()
        trace.record_request("document")
        trace.record_blocked("prefetch")
        trace.record_cache(hit=True)
        summary = trace.summary()
        assert "document" in summary
        assert "blocked:prefetch" in summary
        assert "served from cache" in summary

    def test_summary_without_data(self) -> None:
        assert "nothing observed" in ResourceTrace().summary()
