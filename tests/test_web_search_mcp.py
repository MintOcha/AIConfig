import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

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
    *,
    brave_keys: tuple[str, ...] = (),
    codex_standalone_keys: tuple[str, ...] = (),
    codex_standalone_base_url: str = "https://example.test/v1",
    tavily_keys: tuple[str, ...] = (),
) -> RouterConfig:
    return RouterConfig(
        duckduckgo_command="uvx",
        duckduckgo_args=("duckduckgo-mcp-server==0.6.1",),
        brave_command="npx",
        brave_args=("-y", "@brave/brave-search-mcp-server@2.1.0"),
        brave_keys=brave_keys,
        duckduckgo_enabled=False,
        codex_standalone_keys=codex_standalone_keys,
        codex_standalone_base_url=codex_standalone_base_url,
        codex_standalone_model="gpt-5.6-sol",
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


class FakeBraveClient:
    def __init__(self, request, transport) -> None:
        self.request = request
        self.transport = transport

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def call_tool(self, name, arguments):
        self.request.update(
            name=name,
            arguments=arguments,
            command=self.transport.command,
            args=self.transport.args,
            env=self.transport.env,
        )
        return SimpleNamespace(
            is_error=False,
            structured_content=None,
            data=None,
            content=[
                SimpleNamespace(
                    text=(
                        '{"title":"FastMCP","url":"https://gofastmcp.com",'
                        '"description":"Documentation"}'
                    )
                ),
                SimpleNamespace(
                    text=(
                        '{"title":"MCP","url":"https://modelcontextprotocol.io",'
                        '"description":"Protocol"}'
                    )
                ),
            ],
        )


class FakeBraveFactory:
    def __init__(self, request) -> None:
        self.request = request

    def __call__(self, transport, *, timeout):
        self.request["timeout"] = timeout
        return FakeBraveClient(self.request, transport)


class FakeCodexFactory:
    def __init__(self, payload) -> None:
        self.payload = payload
        self.requests = []

    def __call__(self, *, timeout):
        self.timeout = timeout

        async def handle(request):
            self.requests.append(request)
            return httpx.Response(200, json=self.payload)

        return httpx.AsyncClient(transport=httpx.MockTransport(handle), timeout=timeout)


class KeyRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_codex_standalone_uses_alpha_contract_and_normalizes_results(
        self,
    ) -> None:
        factory = FakeCodexFactory(
            {
                "output": "Aggregated answer",
                "results": [
                    {
                        "type": "text_result",
                        "title": "OpenAI API",
                        "url": "https://platform.openai.com/docs",
                        "snippet": "Developer documentation",
                    }
                ],
            }
        )
        router = SearchRouter(
            router_config(
                codex_standalone_keys=("secret",),
                codex_standalone_base_url="https://example.test/v1/",
            ),
            codex_client_factory=factory,
        )

        results = await router._search_codex_standalone("OpenAI docs", 5)

        request = factory.requests[0]
        body = json.loads(request.content)
        self.assertEqual(str(request.url), "https://example.test/v1/alpha/search")
        self.assertEqual(request.headers["Authorization"], "Bearer secret")
        self.assertEqual(body["model"], "gpt-5.6-sol")
        self.assertEqual(body["commands"], {"search_query": [{"q": "OpenAI docs"}]})
        self.assertTrue(body["id"])
        self.assertEqual(
            results,
            [
                {
                    "title": "OpenAI API",
                    "url": "https://platform.openai.com/docs",
                    "snippet": "Developer documentation",
                }
            ],
        )

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
        router = SearchRouter(router_config(tavily_keys=("tavily",)))
        router._search_duckduckgo = AsyncMock(
            side_effect=DuckDuckGoUnavailable("empty")
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
            brave_client_factory=FakeBraveFactory(request),
        )
        router._search_duckduckgo = AsyncMock(
            side_effect=DuckDuckGoUnavailable("empty")
        )

        result = await router.search("fastmcp", 5)

        self.assertEqual(request["name"], "brave_web_search")
        self.assertEqual(request["arguments"], {"query": "fastmcp", "count": 5})
        self.assertEqual(request["command"], "npx")
        self.assertEqual(
            request["args"], ["-y", "@brave/brave-search-mcp-server@2.1.0"]
        )
        self.assertEqual(request["env"]["BRAVE_API_KEY"], "brave")
        self.assertEqual(
            result,
            [
                {
                    "title": "FastMCP",
                    "url": "https://gofastmcp.com",
                    "snippet": "Documentation",
                },
                {
                    "title": "MCP",
                    "url": "https://modelcontextprotocol.io",
                    "snippet": "Protocol",
                },
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
        router._search_codex_standalone = AsyncMock(
            side_effect=[
                [{"title": "Codex", "url": "https://codex.example", "snippet": ""}],
                ProviderRequestError("failed"),
            ]
        )

        first = await router.search("first", 5)
        second = await router.search("second", 5)
        third = await router.search("third", 5)
        fourth = await router.search("fourth", 5)
        fifth = await router.search("fifth", 5)

        self.assertEqual(first[0]["title"], "Brave")
        self.assertEqual(second[0]["title"], "DDG")
        self.assertEqual(third[0]["title"], "Codex")
        self.assertEqual(fourth[0]["title"], "Tavily")
        self.assertEqual(fifth[0]["title"], "Tavily")
        self.assertEqual(router._search_duckduckgo.await_count, 2)
        self.assertEqual(router._search_brave.await_count, 2)
        self.assertEqual(router._search_codex_standalone.await_count, 2)
        self.assertEqual(router._search_tavily.await_count, 2)

    async def test_search_errors_only_after_all_providers_fail(self) -> None:
        router = SearchRouter(router_config())
        router._search_duckduckgo = AsyncMock(
            side_effect=DuckDuckGoUnavailable("blocked")
        )
        router._search_brave = AsyncMock(side_effect=ProviderRequestError("failed"))
        router._search_codex_standalone = AsyncMock(
            side_effect=ProviderRequestError("failed")
        )
        router._search_tavily = AsyncMock(side_effect=ProviderRequestError("failed"))

        with self.assertRaisesRegex(
            ProviderRequestError, "All web search providers failed"
        ):
            await router.search("query", 5)

        router._search_duckduckgo.assert_awaited_once()
        router._search_brave.assert_awaited_once()
        router._search_codex_standalone.assert_awaited_once()
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
        router._fetch_duckduckgo = AsyncMock(return_value=([], ["https://example.com"]))

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
