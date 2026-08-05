import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).parents[1] / "mcp"))

from web_search_mcp import (
    BraveCooldown,
    BraveKeys,
    DuckDuckGoUnavailable,
    ProviderRequestError,
    RoundRobinKeys,
    RouterConfig,
    SearchRouter,
)


def router_config(
    *, brave_keys: tuple[str, ...] = (), tavily_keys: tuple[str, ...] = ()
) -> RouterConfig:
    return RouterConfig(
        brave_keys=brave_keys,
        tavily_keys=tavily_keys,
        tavily_endpoint="https://mcp.tavily.com/mcp/",
        brave_cooldown_seconds=1.0,
        duckduckgo_cooldown_seconds=60.0,
        request_timeout_seconds=30.0,
    )


class FakeTavilyClient:
    def __init__(self, calls, outcomes, api_key) -> None:
        self.calls = calls
        self.outcomes = outcomes
        self.api_key = api_key

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def call_tool(self, name, arguments):
        self.calls.append((self.api_key, name, arguments))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeTavilyFactory:
    def __init__(self, outcomes) -> None:
        self.calls = []
        self.outcomes = list(outcomes)

    def __call__(self, _endpoint, *, auth, timeout):
        self.timeout = timeout
        return FakeTavilyClient(self.calls, self.outcomes, auth.api_key)


class FakeBraveResponse:
    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {
            "web": {
                "results": [
                    {
                        "title": "FastMCP",
                        "url": "https://gofastmcp.com",
                        "description": "Documentation",
                    }
                ]
            }
        }


class FakeBraveClient:
    def __init__(self, request) -> None:
        self.request = request

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, *, params, headers):
        self.request.update(url=url, params=params, headers=headers)
        return FakeBraveResponse()


class KeyRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_tavily_keys_rotate_in_order(self) -> None:
        keys = RoundRobinKeys(("first", "second"))

        self.assertEqual(await keys.next_key(), "first")
        self.assertEqual(await keys.next_key(), "second")
        self.assertEqual(await keys.next_key(), "first")

    async def test_brave_key_reports_cooldown_before_reuse(self) -> None:
        keys = BraveKeys(("brave",), cooldown_seconds=60)
        await keys.acquire(wait=False)

        with self.assertRaises(BraveCooldown):
            await keys.acquire(wait=False)

    async def test_empty_duckduckgo_response_falls_back_to_tavily(self) -> None:
        router = SearchRouter(
            router_config(tavily_keys=("tavily",)),
            duckduckgo_search=lambda *_args: [],
        )
        router._search_tavily = AsyncMock(
            return_value=[
                {"title": "FastMCP", "url": "https://gofastmcp.com", "snippet": "Docs"}
            ]
        )

        result = await router.search("fastmcp", 5)

        self.assertEqual(
            result,
            [{"title": "FastMCP", "url": "https://gofastmcp.com", "snippet": "Docs"}],
        )
        router._search_tavily.assert_awaited_once_with("fastmcp", 5)

    async def test_generic_search_is_translated_and_normalized_for_brave(self) -> None:
        request = {}
        router = SearchRouter(
            router_config(brave_keys=("brave",)),
            http_client_factory=lambda **_kwargs: FakeBraveClient(request),
            duckduckgo_search=lambda *_args: [],
        )

        result = await router.search("fastmcp", 5)

        self.assertEqual(request["params"], {"q": "fastmcp", "count": 5})
        self.assertEqual(
            result,
            [
                {
                    "title": "FastMCP",
                    "url": "https://gofastmcp.com",
                    "snippet": "Documentation",
                }
            ],
        )

    async def test_providers_rotate_and_failure_advances_to_the_next(self) -> None:
        router = SearchRouter(router_config(tavily_keys=("tavily",)))
        router._search_duckduckgo = AsyncMock(
            side_effect=[
                [{"title": "DDG", "url": "https://ddg.example", "snippet": ""}],
                DuckDuckGoUnavailable("blocked"),
            ]
        )
        router._search_brave = AsyncMock(
            side_effect=[
                [{"title": "Brave", "url": "https://brave.example", "snippet": ""}],
                ProviderRequestError("failed"),
            ]
        )
        router._search_tavily = AsyncMock(
            return_value=[
                {"title": "Tavily", "url": "https://tavily.example", "snippet": ""}
            ]
        )

        first = await router.search("first", 5)
        second = await router.search("second", 5)
        third = await router.search("third", 5)
        fourth = await router.search("fourth", 5)

        self.assertEqual(first[0]["title"], "DDG")
        self.assertEqual(second[0]["title"], "Brave")
        self.assertEqual(third[0]["title"], "Tavily")
        self.assertEqual(fourth[0]["title"], "Tavily")
        self.assertEqual(router._search_duckduckgo.await_count, 2)
        self.assertEqual(router._search_brave.await_count, 2)
        self.assertEqual(router._search_tavily.await_count, 2)

    async def test_search_errors_only_after_all_providers_fail(self) -> None:
        router = SearchRouter(router_config())
        router._search_duckduckgo = AsyncMock(
            side_effect=DuckDuckGoUnavailable("blocked")
        )
        router._search_brave = AsyncMock(side_effect=ProviderRequestError("failed"))
        router._search_tavily = AsyncMock(side_effect=ProviderRequestError("failed"))

        with self.assertRaisesRegex(
            ProviderRequestError, "All web search providers failed"
        ):
            await router.search("query", 5)

        router._search_duckduckgo.assert_awaited_once()
        router._search_brave.assert_awaited_once()
        router._search_tavily.assert_awaited_once()

    async def test_tavily_retries_with_the_next_key(self) -> None:
        factory = FakeTavilyFactory(
            [
                RuntimeError("rate limited"),
                {"results": [{"url": "https://example.com"}]},
            ]
        )
        router = SearchRouter(
            router_config(tavily_keys=("first", "second")),
            tavily_client_factory=factory,
        )

        result = await router._call_tavily("tavily_search", {"query": "example"})

        self.assertEqual(result, {"results": [{"url": "https://example.com"}]})
        self.assertEqual([call[0] for call in factory.calls], ["first", "second"])

    async def test_fetch_and_research_translate_to_tavily_tools(self) -> None:
        factory = FakeTavilyFactory(
            [
                {
                    "results": [
                        {"url": "https://example.com", "raw_content": "# Example"}
                    ]
                },
                {"report": "Research report"},
            ]
        )
        router = SearchRouter(
            router_config(tavily_keys=("tavily",)),
            tavily_client_factory=factory,
        )

        content = await router.fetch_content(["https://example.com"])
        report = await router.research("Compare the options")

        self.assertEqual(
            content, [{"url": "https://example.com", "content": "# Example"}]
        )
        self.assertEqual(report, "Research report")
        self.assertEqual(factory.calls[0][1], "tavily_extract")
        self.assertEqual(factory.calls[0][2]["urls"], ["https://example.com"])
        self.assertEqual(
            factory.calls[1][1:],
            ("tavily_research", {"input": "Compare the options", "model": "auto"}),
        )


if __name__ == "__main__":
    unittest.main()
