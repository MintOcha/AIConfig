import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock


sys.path.insert(0, str(Path(__file__).parents[1] / "mcp"))

from web_search_mcp import (
    BraveCooldown,
    BraveKeys,
    BraveRateLimited,
    RouterConfig,
    RoundRobinKeys,
    SearchRouter,
)


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

    async def test_auto_search_uses_tavily_when_brave_is_cooling_down(self) -> None:
        config = RouterConfig(
            brave_keys=("brave",),
            tavily_keys=("tavily",),
            tavily_endpoint="https://mcp.tavily.com/mcp/",
            brave_cooldown_seconds=1.0,
            brave_cooldown_strategy="tavily",
            request_timeout_seconds=30.0,
        )
        router = SearchRouter(config)
        router._search_brave = AsyncMock(side_effect=BraveCooldown(0.5))
        router._search_tavily = AsyncMock(return_value={"results": []})

        result = await router.search("fastmcp", 10, "basic", "auto")

        self.assertEqual(result["provider"], "tavily")
        self.assertEqual(result["fallback_reason"], "BraveCooldown")
        router._search_tavily.assert_awaited_once()

    async def test_wait_strategy_retries_brave_after_a_rate_limit(self) -> None:
        config = RouterConfig(
            brave_keys=("brave",),
            tavily_keys=("tavily",),
            tavily_endpoint="https://mcp.tavily.com/mcp/",
            brave_cooldown_seconds=1.0,
            brave_cooldown_strategy="wait",
            request_timeout_seconds=30.0,
        )
        router = SearchRouter(config)
        router._search_brave = AsyncMock(
            side_effect=[BraveRateLimited(0), {"web": {"results": []}}]
        )
        router._search_tavily = AsyncMock()

        result = await router.search("fastmcp", 10, "basic", "auto")

        self.assertEqual(result["provider"], "brave")
        self.assertEqual(router._search_brave.await_count, 2)
        router._search_tavily.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
