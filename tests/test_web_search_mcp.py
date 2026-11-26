import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp"))

import asyncio
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastmcp import FastMCP

import web_search_mcp
from web_search_mcp import (
    ProviderRequestError,
    RouterConfig,
    SearchRouter,
    register_tools,
)


def make_dummy_config() -> RouterConfig:
    return RouterConfig(
        duckduckgo_command="duckduckgo-mcp",
        duckduckgo_args=(),
        brave_command="brave-search-mcp",
        brave_args=(),
        brave_keys=("brave-dummy-key",),
        duckduckgo_enabled=True,
        codex_standalone_keys=("codex-dummy-key",),
        codex_standalone_base_url="https://codex.test",
        codex_standalone_model="gpt-4o",
        tavily_keys=(),
        tavily_endpoint="",
        brave_cooldown_seconds=0.0,
        duckduckgo_cooldown_seconds=0.0,
        request_timeout_seconds=5.0,
    )


class TestViewRefTracking(unittest.TestCase):
    def setUp(self):
        self.router = SearchRouter(make_dummy_config())

    def test_record_view_ref_with_cite(self):
        output = "Here is content citeturn0view1 and more text"
        ref = self.router.record_view_ref("https://example.com/page1", output)
        self.assertEqual(ref, "turn0view1")
        self.assertEqual(self.router._url_to_view_ref["https://example.com/page1"], "turn0view1")
        self.assertEqual(self.router._url_to_view_ref["turn0view1"], "turn0view1")
        self.assertEqual(self.router._recent_view_refs, ["turn0view1"])

    def test_record_view_ref_ordering(self):
        self.router.record_view_ref("https://example.com/1", "citeturn0view0")
        self.router.record_view_ref("https://example.com/2", "citeturn0view1")
        self.assertEqual(self.router._recent_view_refs, ["turn0view0", "turn0view1"])

        # Re-visiting turn0view0 should move it to the end (most recent)
        self.router.record_view_ref("https://example.com/1", "citeturn0view0")
        self.assertEqual(self.router._recent_view_refs, ["turn0view1", "turn0view0"])

    def test_resolve_view_ref_empty(self):
        self.assertEqual(self.router.resolve_view_ref(None), "")
        self.assertEqual(self.router.resolve_view_ref(""), "")
        self.assertEqual(self.router.resolve_view_ref("   "), "")

        self.router.record_view_ref("https://example.com", "citeturn0view0")
        self.assertEqual(self.router.resolve_view_ref(None), "turn0view0")
        self.assertEqual(self.router.resolve_view_ref(""), "turn0view0")

    def test_resolve_view_ref_numeric_index(self):
        self.router.record_view_ref("https://example.com/a", "citeturn0view0")
        self.router.record_view_ref("https://example.com/b", "citeturn0view1")

        self.assertEqual(self.router.resolve_view_ref("0"), "turn0view0")
        self.assertEqual(self.router.resolve_view_ref("1"), "turn0view1")
        # Out of bounds falls back to latest opened page
        self.assertEqual(self.router.resolve_view_ref("99"), "turn0view1")
        self.assertEqual(self.router.resolve_view_ref("-1"), "turn0view1")

    def test_resolve_view_ref_urls_and_turn_refs(self):
        self.router.record_view_ref("https://example.com/mapped", "citeturn0view5")
        self.assertEqual(self.router.resolve_view_ref("https://example.com/mapped"), "turn0view5")
        self.assertEqual(self.router.resolve_view_ref("https://example.com/unmapped"), "https://example.com/unmapped")
        self.assertEqual(self.router.resolve_view_ref("turn0view2"), "turn0view2")
        self.assertEqual(self.router.resolve_view_ref("citeturn0view3"), "turn0view3")


class TestEnsureViewRef(unittest.IsolatedAsyncioTestCase):
    async def test_ensure_view_ref_already_resolved(self):
        router = SearchRouter(make_dummy_config())
        router.record_view_ref("https://example.com", "citeturn0view0")

        # Directly passes turn0view0
        ref = await router.ensure_view_ref("turn0view0")
        self.assertEqual(ref, "turn0view0")

        # Resolves mapped URL
        ref2 = await router.ensure_view_ref("https://example.com")
        self.assertEqual(ref2, "turn0view0")

        # Resolves "0"
        ref3 = await router.ensure_view_ref("0")
        self.assertEqual(ref3, "turn0view0")

        # Resolves None (latest)
        ref4 = await router.ensure_view_ref(None)
        self.assertEqual(ref4, "turn0view0")

    async def test_ensure_view_ref_auto_opens_unmapped_url(self):
        router = SearchRouter(make_dummy_config())
        mock_execute = AsyncMock(return_value={"output": "Opened page citeturn1view0\nL1: Content"})
        router.execute_codex_command = mock_execute

        ref = await router.ensure_view_ref("https://newsite.com")
        self.assertEqual(ref, "turn1view0")
        mock_execute.assert_awaited_once_with("open", [{"ref_id": "https://newsite.com"}])
        self.assertEqual(router._url_to_view_ref["https://newsite.com"], "turn1view0")
        self.assertIn("turn1view0", router._recent_view_refs)

    async def test_ensure_view_ref_error_when_no_active_page(self):
        router = SearchRouter(make_dummy_config())
        with self.assertRaises(ProviderRequestError) as ctx:
            await router.ensure_view_ref(None)
        self.assertIn("No active page reference available", str(ctx.exception))

        with self.assertRaises(ProviderRequestError) as ctx2:
            await router.ensure_view_ref("invalid_ref")
        self.assertIn("Could not resolve page reference", str(ctx2.exception))


class TestFetchContentSharedSession(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_single_codex_uses_shared_session_and_records_ref(self):
        router = SearchRouter(make_dummy_config())
        url = "https://example.com/test"

        captured_requests = []

        class MockResponse:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "output": "citeturn0view9\nL1: Line one\nL2: Line two\n"
                }

        class MockClient:
            async def post(self, endpoint, headers=None, json=None):
                captured_requests.append(json)
                return MockResponse()

        res = await router._fetch_single_codex(url, MockClient(), "dummy-key")
        self.assertEqual(res, "Line one\nLine two")
        self.assertEqual(len(captured_requests), 1)
        # Shared session id check
        self.assertEqual(captured_requests[0]["id"], router.session_id)
        # Ref was recorded
        self.assertEqual(router._url_to_view_ref[url], "turn0view9")
        self.assertIn("turn0view9", router._recent_view_refs)

    async def test_fetch_content_returns_ref_id(self):
        router = SearchRouter(make_dummy_config())
        urls = ["https://example.com/1", "https://example.com/2"]

        router.record_view_ref("https://example.com/1", "citeturn0view1")
        router.record_view_ref("https://example.com/2", "citeturn0view2")

        async def mock_fetch_codex(u_list):
            return [
                {"url": "https://example.com/1", "content": "Content 1", "ref_id": "turn0view1"},
                {"url": "https://example.com/2", "content": "Content 2", "ref_id": "turn0view2"},
            ], []

        router._fetch_codex_standalone = mock_fetch_codex

        results = await router.fetch_content(urls)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], {"url": "https://example.com/1", "content": "Content 1", "ref_id": "turn0view1"})
        self.assertEqual(results[1], {"url": "https://example.com/2", "content": "Content 2", "ref_id": "turn0view2"})


class TestToolRegistrations(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = make_dummy_config()
        self.router = SearchRouter(self.cfg)
        self.app = FastMCP("TestApp")

        # Set module-level router for registered tools
        web_search_mcp.router = self.router
        register_tools(self.app, self.cfg)

    def tearDown(self):
        web_search_mcp.router = None

    async def test_click_link_argument_flexibility(self):
        # Retrieve click_link tool
        click_link_tool = await self.app.get_tool("click-link")

        # Mock execute_codex_command
        mock_execute = AsyncMock(return_value={"output": "Clicked citeturn0view3"})
        self.router.execute_codex_command = mock_execute

        # 1. Using link_id and ref_id
        res = await click_link_tool.run({"link_id": 10, "ref_id": "turn0view0"})
        self.assertEqual(res.content[0].text, "Clicked citeturn0view3")
        mock_execute.assert_awaited_with("click", [{"ref_id": "turn0view0", "id": 10}])

        # 2. Using id and url (unmapped, auto-opens)
        mock_execute.reset_mock()
        mock_execute.side_effect = [
            {"output": "Opened citeturn0view4"},  # open call
            {"output": "Clicked link"},              # click call
        ]
        res2 = await click_link_tool.run({"id": 25, "url": "https://example.com/target"})
        self.assertEqual(res2.content[0].text, "Clicked link")
        self.assertEqual(mock_execute.await_count, 2)
        mock_execute.assert_awaited_with("click", [{"ref_id": "turn0view4", "id": 25}])

        # 3. Missing link identifier
        with self.assertRaises(ValueError) as ctx:
            await click_link_tool.run({"ref_id": "turn0view0"})
        self.assertIn("click-link requires a link identifier", str(ctx.exception))

    async def test_find_in_page_argument_flexibility(self):
        find_tool = await self.app.get_tool("find-in-page")

        mock_execute = AsyncMock(return_value={"output": "Found match citeturn0view5"})
        self.router.execute_codex_command = mock_execute

        # 1. Using pattern and ref_id="0"
        self.router.record_view_ref("https://example.com", "citeturn0view1")
        res = await find_tool.run({"pattern": "pricing", "ref_id": "0"})
        self.assertEqual(res.content[0].text, "Found match citeturn0view5")
        mock_execute.assert_awaited_with("find", [{"ref_id": "turn0view1", "pattern": "pricing"}])

        # 2. Using query instead of pattern, and url
        mock_execute.reset_mock()
        res2 = await find_tool.run({"query": "faq", "url": "https://example.com"})
        self.assertEqual(res2.content[0].text, "Found match citeturn0view5")
        mock_execute.assert_awaited_with("find", [{"ref_id": "turn0view1", "pattern": "faq"}])

        # 3. Using text instead of pattern
        mock_execute.reset_mock()
        res3 = await find_tool.run({"text": "contact"})
        self.assertEqual(res3.content[0].text, "Found match citeturn0view5")
        mock_execute.assert_awaited_with("find", [{"ref_id": "turn0view5", "pattern": "contact"}])

        # 4. Missing pattern/query/text
        with self.assertRaises(ValueError) as ctx:
            await find_tool.run({"ref_id": "0"})
        self.assertIn("find-in-page requires a search pattern", str(ctx.exception))

    async def test_open_page_argument_flexibility(self):
        open_tool = await self.app.get_tool("open-page")

        mock_execute = AsyncMock(return_value={"output": "Opened citeturn0view8"})
        self.router.execute_codex_command = mock_execute

        # 1. Using url and lineno as string
        res = await open_tool.run({"url": "https://example.com/docs", "lineno": "42"})
        self.assertEqual(res.content[0].text, "Opened citeturn0view8")
        mock_execute.assert_awaited_with("open", [{"ref_id": "https://example.com/docs", "lineno": 42}])

        # 2. Using ref_id="0"
        mock_execute.reset_mock()
        res2 = await open_tool.run({"ref_id": "0"})
        self.assertEqual(res2.content[0].text, "Opened citeturn0view8")
        mock_execute.assert_awaited_with("open", [{"ref_id": "turn0view8"}])

        # 3. Neither ref_id nor url
        with self.assertRaises(ValueError) as ctx:
            await open_tool.run({})
        self.assertIn("open-page requires either 'ref_id' or 'url'", str(ctx.exception))

    async def test_fetch_tool_returns_ref_id(self):
        fetch_tool = await self.app.get_tool("fetch")

        async def mock_fetch(urls):
            return [{"url": u, "content": f"Text for {u}", "ref_id": "turn0view1"} for u in urls]

        self.router.fetch_content = mock_fetch

        # Test with single 'url'
        res = await fetch_tool.run({"url": "https://example.com/single"})
        # Tool result should contain structured or JSON output
        self.assertIn("turn0view1", res.content[0].text)

        # Test with 'urls' list
        res2 = await fetch_tool.run({"urls": ["https://example.com/multi"]})
        self.assertIn("turn0view1", res2.content[0].text)

        # Missing both url and urls
        with self.assertRaises(ValueError) as ctx:
            await fetch_tool.run({})
        self.assertIn("fetch requires either 'urls'", str(ctx.exception))

    async def test_click_link_and_find_in_page_error_handling(self):
        click_tool = await self.app.get_tool("click-link")
        find_tool = await self.app.get_tool("find-in-page")

        # Empty recent refs and no target
        with self.assertRaises(ProviderRequestError) as ctx:
            await click_tool.run({"link_id": 1})
        self.assertIn("No active page reference available", str(ctx.exception))

        with self.assertRaises(ProviderRequestError) as ctx2:
            await find_tool.run({"pattern": "test"})
        self.assertIn("No active page reference available", str(ctx2.exception))

        # Whitespace pattern
        with self.assertRaises(ValueError) as ctx3:
            await find_tool.run({"pattern": "   "})
        self.assertIn("find-in-page requires a search pattern", str(ctx3.exception))


class TestCodexModelResolutionAndFailover(unittest.IsolatedAsyncioTestCase):
    async def test_codex_discovers_model_when_config_model_empty(self):
        cfg = make_dummy_config()
        cfg = RouterConfig(**{**cfg.__dict__, "codex_standalone_model": ""})
        seen_posts = []
        get_calls = 0

        class MockClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url, headers=None):
                nonlocal get_calls
                get_calls += 1
                req = httpx.Request("GET", url)
                return httpx.Response(
                    200,
                    request=req,
                    json={
                        "data": [
                            {"id": "gpt-image-2", "owned_by": "openai"},
                            {"id": "nvidia/nemotron", "owned_by": "nvidia"},
                            {"id": "gpt-5.6-sol", "owned_by": "openai"},
                        ]
                    },
                )

            async def post(self, url, headers=None, json=None):
                seen_posts.append(json)
                req = httpx.Request("POST", url)
                return httpx.Response(
                    200,
                    request=req,
                    json={
                        "results": [
                            {"title": "Result", "url": "https://example.com", "snippet": "Snippet"}
                        ]
                    },
                )

        router = SearchRouter(cfg, codex_client_factory=lambda **kw: MockClient())
        results = await router._search_codex_standalone("python", 5)
        self.assertEqual(len(results), 1)
        self.assertEqual(seen_posts[0]["model"], "gpt-5.6-sol")

        # Second call reuses cached model without querying /models again.
        await router._search_codex_standalone("python 2", 5)
        self.assertEqual(get_calls, 1)
        self.assertEqual(seen_posts[1]["model"], "gpt-5.6-sol")

    async def test_codex_omits_empty_model_when_discovery_unavailable_and_retries_on_400(self):
        cfg = make_dummy_config()
        cfg = RouterConfig(**{**cfg.__dict__, "codex_standalone_model": "stale-model"})
        seen_models = []

        class MockClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url, headers=None):
                req = httpx.Request("GET", url)
                return httpx.Response(
                    200,
                    request=req,
                    json={
                        "data": [
                            {"id": "stale-model", "owned_by": "openai"},
                            {"id": "gpt-6.1-sol", "owned_by": "openai"},
                        ]
                    },
                )

            async def post(self, url, headers=None, json=None):
                seen_models.append(json.get("model"))
                req = httpx.Request("POST", url)
                if json.get("model") == "stale-model":
                    return httpx.Response(
                        400,
                        request=req,
                        json={
                            "error": {
                                "message": "Invalid model ID.",
                                "type": "invalid_request_error",
                                "param": "model",
                                "code": "invalid_value",
                            }
                        },
                    )
                return httpx.Response(
                    200,
                    request=req,
                    json={
                        "results": [
                            {"title": "OK", "url": "https://example.org", "snippet": "Worked"}
                        ]
                    },
                )

        router = SearchRouter(cfg, codex_client_factory=lambda **kw: MockClient())
        results = await router._search_codex_standalone("query", 3)
        self.assertEqual(seen_models, ["stale-model", "gpt-6.1-sol"])
        self.assertEqual(results[0]["url"], "https://example.org")

        # When discovery fails and configured model is empty, "model" key is omitted.
        cfg_empty = RouterConfig(**{**cfg.__dict__, "codex_standalone_model": ""})
        seen_empty_payloads = []

        class NoModelsClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url, headers=None):
                req = httpx.Request("GET", url)
                return httpx.Response(404, request=req)

            async def post(self, url, headers=None, json=None):
                seen_empty_payloads.append(json)
                req = httpx.Request("POST", url)
                return httpx.Response(200, request=req, json={"results": []})

        router_no_models = SearchRouter(cfg_empty, codex_client_factory=lambda **kw: NoModelsClient())
        self.assertEqual(await router_no_models._search_codex_standalone("query", 3), [])
        self.assertNotIn("model", seen_empty_payloads[0])

    async def test_search_fails_over_on_codex_400_to_duckduckgo(self):
        cfg = RouterConfig(
            duckduckgo_command="duckduckgo-mcp",
            duckduckgo_args=(),
            brave_command="brave-search-mcp",
            brave_args=(),
            brave_keys=(),
            duckduckgo_enabled=True,
            codex_standalone_keys=("secret-codex-key",),
            codex_standalone_base_url="https://codex.test/v1",
            codex_standalone_model="",
            tavily_keys=(),
            tavily_endpoint="",
            brave_cooldown_seconds=0.0,
            duckduckgo_cooldown_seconds=60.0,
            request_timeout_seconds=5.0,
        )

        class FailingCodexClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url, headers=None):
                return httpx.Response(404, request=httpx.Request("GET", url))

            async def post(self, url, headers=None, json=None):
                return httpx.Response(
                    400,
                    request=httpx.Request("POST", url),
                    json={"error": {"message": "Invalid model ID.", "param": "model"}},
                )

        ddg_result = MagicMock(
            is_error=False,
            structured_content=None,
            data=None,
            content=[
                MagicMock(
                    text="1. Fallback Hit\n   URL: https://ddg.example/hit\n   Summary: DDG fallback worked"
                )
            ],
        )

        class WorkingDDGClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def call_tool(self, name, args):
                return ddg_result

        router = SearchRouter(
            cfg,
            codex_client_factory=lambda **kw: FailingCodexClient(),
            duckduckgo_client_factory=lambda *a, **kw: WorkingDDGClient(),
        )
        results = await router.search("test query", 5)
        self.assertEqual(
            results,
            [{"title": "Fallback Hit", "url": "https://ddg.example/hit", "snippet": "DDG fallback worked"}],
        )

    async def test_search_fails_over_when_duckduckgo_returns_error_text_or_exception(self):
        cfg = RouterConfig(
            duckduckgo_command="duckduckgo-mcp",
            duckduckgo_args=(),
            brave_command="brave-search-mcp",
            brave_args=(),
            brave_keys=(),
            duckduckgo_enabled=True,
            codex_standalone_keys=("secret-codex-key",),
            codex_standalone_base_url="https://codex.test/v1",
            codex_standalone_model="gpt-5.6-sol",
            tavily_keys=(),
            tavily_endpoint="",
            brave_cooldown_seconds=0.0,
            duckduckgo_cooldown_seconds=60.0,
            request_timeout_seconds=5.0,
        )

        class WorkingCodexClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, headers=None, json=None):
                return httpx.Response(
                    200,
                    request=httpx.Request("POST", url),
                    json={
                        "results": [
                            {"title": "Codex Hit", "url": "https://codex.example/hit", "snippet": "Codex worked"}
                        ]
                    },
                )

        ddg_error_result = MagicMock(
            is_error=False,
            structured_content=None,
            data=None,
            content=[MagicMock(text="An error occurred while searching: upstream 429")],
        )

        class ErrorTextDDGClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def call_tool(self, name, args):
                return ddg_error_result

        router = SearchRouter(
            cfg,
            codex_client_factory=lambda **kw: WorkingCodexClient(),
            duckduckgo_client_factory=lambda *a, **kw: ErrorTextDDGClient(),
        )
        # Force duckduckgo first on this search call.
        router._next_provider_index = 1
        results = await router.search("test query", 5)
        self.assertEqual(results[0]["url"], "https://codex.example/hit")

    async def test_search_exhausted_reports_all_provider_outcomes_and_redacts_secrets(self):
        cfg = RouterConfig(
            duckduckgo_command="duckduckgo-mcp",
            duckduckgo_args=(),
            brave_command="brave-search-mcp",
            brave_args=(),
            brave_keys=(),
            duckduckgo_enabled=True,
            codex_standalone_keys=("super-secret-token-123",),
            codex_standalone_base_url="https://codex.test/v1",
            codex_standalone_model="gpt-5.6-sol",
            tavily_keys=(),
            tavily_endpoint="",
            brave_cooldown_seconds=0.0,
            duckduckgo_cooldown_seconds=60.0,
            request_timeout_seconds=5.0,
        )

        class FailingCodexClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, headers=None, json=None):
                raise RuntimeError("Bearer super-secret-token-123 failed")

        ddg_empty = MagicMock(
            is_error=False,
            structured_content=None,
            data=None,
            content=[MagicMock(text="No results were found for your search query.")],
        )

        class EmptyDDGClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def call_tool(self, name, args):
                return ddg_empty

        router = SearchRouter(
            cfg,
            codex_client_factory=lambda **kw: FailingCodexClient(),
            duckduckgo_client_factory=lambda *a, **kw: EmptyDDGClient(),
        )
        with self.assertRaises(ProviderRequestError) as ctx:
            await router.search("query", 5)
        msg = str(ctx.exception)
        self.assertIn("[codex_standalone]", msg)
        self.assertIn("[duckduckgo]", msg)
        self.assertIn("Returned 0 results", msg)
        self.assertNotIn("super-secret-token-123", msg)

if __name__ == "__main__":
    unittest.main()
