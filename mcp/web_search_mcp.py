#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "fastmcp==3.4.5",
#   "httpx==0.28.1",
# ]
# ///

import argparse
import asyncio
import time
import tomllib
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
from fastmcp import Client, FastMCP


BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"


class ConfigurationError(ValueError):
    pass


class ProviderRequestError(RuntimeError):
    pass


class BraveCooldown(RuntimeError):
    def __init__(self, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        super().__init__("Brave is cooling down")


class BraveRateLimited(ProviderRequestError):
    def __init__(self, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        super().__init__("Brave rate limited the request")


@dataclass(frozen=True)
class RouterConfig:
    brave_keys: tuple[str, ...]
    tavily_keys: tuple[str, ...]
    tavily_endpoint: str
    brave_cooldown_seconds: float
    brave_cooldown_strategy: Literal["tavily", "wait"]
    request_timeout_seconds: float


@dataclass(frozen=True)
class KeyLease:
    index: int
    value: str


class BearerAuth(httpx.Auth):
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, None, None]:
        request.headers["Authorization"] = f"Bearer {self.api_key}"
        yield request


class RoundRobinKeys:
    def __init__(self, keys: tuple[str, ...]) -> None:
        self._keys = keys
        self._next_index = 0
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._keys)

    async def next_key(self) -> str:
        if not self._keys:
            raise ConfigurationError("No API keys are configured")
        async with self._lock:
            key = self._keys[self._next_index]
            self._next_index = (self._next_index + 1) % len(self._keys)
            return key


class BraveKeys:
    def __init__(self, keys: tuple[str, ...], cooldown_seconds: float) -> None:
        self._keys = keys
        self._available_at = [0.0] * len(keys)
        self._cooldown_seconds = cooldown_seconds
        self._next_index = 0
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._keys)

    async def acquire(self, wait: bool) -> KeyLease:
        if not self._keys:
            raise ConfigurationError("No Brave API keys are configured")

        while True:
            async with self._lock:
                now = time.monotonic()
                for offset in range(len(self._keys)):
                    index = (self._next_index + offset) % len(self._keys)
                    if self._available_at[index] <= now:
                        self._available_at[index] = now + self._cooldown_seconds
                        self._next_index = (index + 1) % len(self._keys)
                        return KeyLease(index=index, value=self._keys[index])
                delay_seconds = max(0.0, min(self._available_at) - now)

            if not wait:
                raise BraveCooldown(delay_seconds)
            await asyncio.sleep(delay_seconds)

    async def defer(self, lease: KeyLease, delay_seconds: float) -> None:
        async with self._lock:
            self._available_at[lease.index] = max(
                self._available_at[lease.index], time.monotonic() + delay_seconds
            )


def _api_keys(data: dict[str, Any], provider: str) -> tuple[str, ...]:
    keys = data.get("api_keys", [])
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        raise ConfigurationError(f"providers.{provider}.api_keys must be a list of strings")

    return tuple(dict.fromkeys(key.strip() for key in keys if key.strip()))


def _number(data: dict[str, Any], key: str, default: float) -> float:
    value = data.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ConfigurationError(f"search.{key} must be a positive number")
    return float(value)


def load_config(path: Path) -> RouterConfig:
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except FileNotFoundError as error:
        raise ConfigurationError(f"Configuration file not found: {path}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigurationError(f"Invalid TOML in {path}: {error}") from error

    search = data.get("search", {})
    providers = data.get("providers", {})
    if not isinstance(search, dict) or not isinstance(providers, dict):
        raise ConfigurationError("Configuration requires [search] and [providers] tables")

    brave = providers.get("brave", {})
    tavily = providers.get("tavily", {})
    if not isinstance(brave, dict) or not isinstance(tavily, dict):
        raise ConfigurationError("providers.brave and providers.tavily must be tables")

    strategy = search.get("brave_cooldown_strategy", "tavily")
    if strategy not in {"tavily", "wait"}:
        raise ConfigurationError('search.brave_cooldown_strategy must be "tavily" or "wait"')

    endpoint = tavily.get("endpoint", "https://mcp.tavily.com/mcp/")
    if not isinstance(endpoint, str) or not endpoint.startswith(("https://", "http://")):
        raise ConfigurationError("providers.tavily.endpoint must be an HTTP(S) URL")

    return RouterConfig(
        brave_keys=_api_keys(brave, "brave"),
        tavily_keys=_api_keys(tavily, "tavily"),
        tavily_endpoint=endpoint,
        brave_cooldown_seconds=_number(search, "brave_cooldown_seconds", 1.0),
        brave_cooldown_strategy=strategy,
        request_timeout_seconds=_number(search, "request_timeout_seconds", 30.0),
    )


def _retry_after(response: httpx.Response) -> float:
    for header in ("Retry-After", "X-RateLimit-Reset"):
        try:
            delay = float(response.headers[header])
        except (KeyError, ValueError):
            continue
        if delay > 0:
            return delay
    return 1.0


def _response(provider: str, result: Any, fallback_reason: str | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {"provider": provider, "result": result}
    if fallback_reason:
        response["fallback_reason"] = fallback_reason
    return response


class SearchRouter:
    def __init__(
        self,
        config: RouterConfig,
        *,
        http_client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
        tavily_client_factory: Callable[..., Client] = Client,
    ) -> None:
        self.config = config
        self._brave_keys = BraveKeys(config.brave_keys, config.brave_cooldown_seconds)
        self._tavily_keys = RoundRobinKeys(config.tavily_keys)
        self._http_client_factory = http_client_factory
        self._tavily_client_factory = tavily_client_factory

    async def _search_brave(
        self,
        query: str,
        max_results: int,
        wait_for_cooldown: bool,
    ) -> dict[str, Any]:
        lease = await self._brave_keys.acquire(wait=wait_for_cooldown)
        try:
            async with self._http_client_factory(timeout=self.config.request_timeout_seconds) as client:
                response = await client.get(
                    BRAVE_SEARCH_URL,
                    params={"q": query, "count": max_results},
                    headers={"Accept": "application/json", "X-Subscription-Token": lease.value},
                )
        except httpx.HTTPError as error:
            raise ProviderRequestError("Brave search request failed") from error

        if response.status_code == 429:
            delay_seconds = _retry_after(response)
            await self._brave_keys.defer(lease, delay_seconds)
            raise BraveRateLimited(delay_seconds)

        try:
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ProviderRequestError(
                f"Brave search failed with HTTP {response.status_code}"
            ) from error

    async def _search_tavily(
        self,
        query: str,
        max_results: int,
        search_depth: Literal["basic", "advanced", "fast", "ultra-fast"],
    ) -> Any:
        api_key = await self._tavily_keys.next_key()
        try:
            async with self._tavily_client_factory(
                self.config.tavily_endpoint,
                auth=BearerAuth(api_key),
                timeout=self.config.request_timeout_seconds,
            ) as client:
                return await client.call_tool(
                    "tavily_search",
                    {
                        "query": query,
                        "max_results": max_results,
                        "search_depth": search_depth,
                    },
                )
        except Exception as error:
            raise ProviderRequestError("Tavily MCP proxy request failed") from error

    async def search(
        self,
        query: str,
        max_results: int,
        search_depth: Literal["basic", "advanced", "fast", "ultra-fast"],
        provider: Literal["auto", "brave", "tavily"],
    ) -> dict[str, Any]:
        if provider == "tavily":
            return _response(
                "tavily",
                await self._search_tavily(query, max_results, search_depth),
            )

        if provider == "brave":
            return _response(
                "brave",
                await self._search_brave(query, max_results, wait_for_cooldown=True),
            )

        if self._brave_keys.configured:
            wait_for_cooldown = (
                self.config.brave_cooldown_strategy == "wait"
                or not self._tavily_keys.configured
            )
            while True:
                try:
                    return _response(
                        "brave",
                        await self._search_brave(query, max_results, wait_for_cooldown),
                    )
                except (BraveCooldown, BraveRateLimited) as error:
                    if (
                        self._tavily_keys.configured
                        and self.config.brave_cooldown_strategy == "tavily"
                    ):
                        return _response(
                            "tavily",
                            await self._search_tavily(query, max_results, search_depth),
                            fallback_reason=error.__class__.__name__,
                        )
                    if isinstance(error, BraveRateLimited):
                        await asyncio.sleep(error.delay_seconds)
                except ProviderRequestError:
                    if not self._tavily_keys.configured:
                        raise
                    return _response(
                        "tavily",
                        await self._search_tavily(query, max_results, search_depth),
                        fallback_reason="BraveRequestFailed",
                    )

        if self._tavily_keys.configured:
            return _response(
                "tavily",
                await self._search_tavily(query, max_results, search_depth),
            )

        raise ConfigurationError(
            "Configure at least one Brave or Tavily API key before searching"
        )


mcp = FastMCP(
    "Web Search Router",
    instructions=(
        "Searches with Brave first, then proxies to Tavily's remote MCP when "
        "Brave is cooling down or unavailable."
    ),
)
router: SearchRouter | None = None


@mcp.tool(name="web_search")
async def web_search(
    query: str,
    max_results: int = 10,
    search_depth: Literal["basic", "advanced", "fast", "ultra-fast"] = "basic",
    provider: Literal["auto", "brave", "tavily"] = "auto",
) -> dict[str, Any]:
    """Search the web with Brave or Tavily through one cooldown-aware tool.

    Auto uses Brave when a key is ready. If all Brave keys are in their configured
    cooldown, it either proxies to Tavily's remote MCP or waits, according to the
    local configuration. Tavily keys are selected round-robin.
    """
    if not query.strip():
        raise ValueError("query must not be empty")
    if not 1 <= max_results <= 20:
        raise ValueError("max_results must be between 1 and 20")
    if router is None:
        raise RuntimeError("Web search router has not been configured")

    return await router.search(query, max_results, search_depth, provider)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Web Search Router MCP server")
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Path to the machine-local TOML configuration file",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate configuration and exit without starting the MCP server",
    )
    return parser.parse_args()


def main() -> None:
    arguments = parse_arguments()
    config = load_config(arguments.config.expanduser())
    if arguments.check_config:
        print(
            "Configuration is valid "
            f"(Brave keys: {len(config.brave_keys)}, Tavily keys: {len(config.tavily_keys)})"
        )
        return

    global router
    router = SearchRouter(config)
    mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
