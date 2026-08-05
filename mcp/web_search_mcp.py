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
import json
import re
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import tomllib
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StdioTransport

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
SEARCH_PROVIDERS = ("duckduckgo", "brave", "tavily")


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


class DuckDuckGoUnavailable(ProviderRequestError):
    pass


@dataclass(frozen=True)
class RouterConfig:
    duckduckgo_command: str
    duckduckgo_args: tuple[str, ...]
    brave_keys: tuple[str, ...]
    tavily_keys: tuple[str, ...]
    tavily_endpoint: str
    brave_cooldown_seconds: float
    duckduckgo_cooldown_seconds: float
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

    @property
    def count(self) -> int:
        return len(self._keys)

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


class CooldownGate:
    def __init__(self, cooldown_seconds: float) -> None:
        self._available_at = 0.0
        self._cooldown_seconds = cooldown_seconds
        self._lock = asyncio.Lock()

    async def enter(self) -> None:
        async with self._lock:
            if self._available_at > time.monotonic():
                raise DuckDuckGoUnavailable("DuckDuckGo is cooling down")

    async def defer(self) -> None:
        async with self._lock:
            self._available_at = time.monotonic() + self._cooldown_seconds


def _api_keys(data: dict[str, Any], provider: str) -> tuple[str, ...]:
    keys = data.get("api_keys", [])
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        raise ConfigurationError(
            f"providers.{provider}.api_keys must be a list of strings"
        )

    return tuple(dict.fromkeys(key.strip() for key in keys if key.strip()))


def _number(data: dict[str, Any], key: str, default: float) -> float:
    value = data.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ConfigurationError(f"search.{key} must be a positive number")
    return float(value)


def _command(provider: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    command = provider.get("command", "uvx")
    args = provider.get("args", ["duckduckgo-mcp-server==0.6.1"])
    if not isinstance(command, str) or not command.strip():
        raise ConfigurationError("providers.duckduckgo.command must be a string")
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise ConfigurationError("providers.duckduckgo.args must be a list of strings")
    return command.strip(), tuple(args)


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
        raise ConfigurationError(
            "Configuration requires [search] and [providers] tables"
        )

    duckduckgo = providers.get("duckduckgo", {})
    brave = providers.get("brave", {})
    tavily = providers.get("tavily", {})
    if not all(isinstance(provider, dict) for provider in (duckduckgo, brave, tavily)):
        raise ConfigurationError("each configured provider must be a table")

    duckduckgo_command, duckduckgo_args = _command(duckduckgo)

    endpoint = tavily.get("endpoint", "https://mcp.tavily.com/mcp/")
    if not isinstance(endpoint, str) or not endpoint.startswith(
        ("https://", "http://")
    ):
        raise ConfigurationError("providers.tavily.endpoint must be an HTTP(S) URL")

    return RouterConfig(
        duckduckgo_command=duckduckgo_command,
        duckduckgo_args=duckduckgo_args,
        brave_keys=_api_keys(brave, "brave"),
        tavily_keys=_api_keys(tavily, "tavily"),
        tavily_endpoint=endpoint,
        brave_cooldown_seconds=_number(search, "brave_cooldown_seconds", 1.0),
        duckduckgo_cooldown_seconds=_number(
            search, "duckduckgo_cooldown_seconds", 60.0
        ),
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


def _tool_result_data(result: Any) -> Any:
    if getattr(result, "is_error", False):
        raise ProviderRequestError("Provider returned a tool error")

    for attribute in ("structured_content", "data"):
        value = getattr(result, attribute, None)
        if value is not None:
            return value

    content = getattr(result, "content", None)
    if isinstance(content, list) and len(content) == 1:
        text = getattr(content[0], "text", None)
        if isinstance(text, str):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    return result


def _result_text(payload: Any) -> str | None:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        result = payload.get("result")
        if isinstance(result, str):
            return result
    result = getattr(payload, "result", None)
    if isinstance(result, str):
        return result
    return None


_DUCKDUCKGO_RESULT = re.compile(
    r"(?:^|\n\n)\d+\. (?P<title>.*?)\n\s+URL: (?P<url>.*?)"
    r"\n\s+Summary: (?P<snippet>.*?)(?=\n\n\d+\. |\Z)",
    re.DOTALL,
)


def _normalize_duckduckgo_results(payload: Any) -> list[dict[str, str]]:
    normalized = _normalize_search_results(
        payload, url_field="url", snippet_field="description"
    )
    if normalized:
        return normalized

    text = _result_text(payload)
    if text is None:
        return []
    return [
        {
            "title": match.group("title").strip(),
            "url": match.group("url").strip(),
            "snippet": match.group("snippet").strip(),
        }
        for match in _DUCKDUCKGO_RESULT.finditer(text)
    ]


def _result_list(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, list):
            return results
        web = payload.get("web")
        if isinstance(web, dict) and isinstance(web.get("results"), list):
            return web["results"]
    return []


def _normalize_search_results(
    payload: Any,
    *,
    url_field: str,
    snippet_field: str,
) -> list[dict[str, str]]:
    normalized = []
    for result in _result_list(payload):
        if not isinstance(result, dict):
            continue
        url = result.get(url_field)
        if not isinstance(url, str) or not url:
            continue
        title = result.get("title", "")
        snippet = result.get(snippet_field, "")
        normalized.append(
            {
                "title": title if isinstance(title, str) else str(title),
                "url": url,
                "snippet": snippet if isinstance(snippet, str) else str(snippet),
            }
        )
    return normalized


def _normalize_extracted_content(payload: Any) -> list[dict[str, str]]:
    normalized = []
    for result in _result_list(payload):
        if not isinstance(result, dict):
            continue
        url = result.get("url")
        content = result.get("raw_content", result.get("content"))
        if isinstance(url, str) and isinstance(content, str):
            normalized.append({"url": url, "content": content})
    return normalized


def _normalize_research(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for field in ("report", "content", "result"):
            value = payload.get(field)
            if isinstance(value, str):
                return value
    return json.dumps(payload, ensure_ascii=False)


def _validate_urls(urls: list[str]) -> list[str]:
    if not 1 <= len(urls) <= 20:
        raise ValueError("urls must contain between 1 and 20 entries")
    normalized = []
    for url in urls:
        if not isinstance(url, str) or urlsplit(url).scheme not in {"http", "https"}:
            raise ValueError("every URL must be an HTTP(S) URL")
        normalized.append(url)
    return normalized


class SearchRouter:
    def __init__(
        self,
        config: RouterConfig,
        *,
        http_client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
        tavily_client_factory: Callable[..., Client] = Client,
        duckduckgo_client_factory: Callable[..., Client] = Client,
    ) -> None:
        self.config = config
        self._brave_keys = BraveKeys(config.brave_keys, config.brave_cooldown_seconds)
        self._tavily_keys = RoundRobinKeys(config.tavily_keys)
        self._duckduckgo_gate = CooldownGate(config.duckduckgo_cooldown_seconds)
        self._next_provider_index = 0
        self._provider_lock = asyncio.Lock()
        self._http_client_factory = http_client_factory
        self._tavily_client_factory = tavily_client_factory
        self._duckduckgo_client_factory = duckduckgo_client_factory

    def _duckduckgo_client(self) -> Client:
        transport = StdioTransport(
            command=self.config.duckduckgo_command,
            args=list(self.config.duckduckgo_args),
        )
        return self._duckduckgo_client_factory(
            transport, timeout=self.config.request_timeout_seconds
        )

    async def _search_brave(
        self,
        query: str,
        max_results: int,
        wait_for_cooldown: bool,
    ) -> list[dict[str, str]]:
        lease = await self._brave_keys.acquire(wait=wait_for_cooldown)
        try:
            async with self._http_client_factory(
                timeout=self.config.request_timeout_seconds
            ) as client:
                response = await client.get(
                    BRAVE_SEARCH_URL,
                    params={"q": query, "count": max_results},
                    headers={
                        "Accept": "application/json",
                        "X-Subscription-Token": lease.value,
                    },
                )
        except httpx.HTTPError as error:
            raise ProviderRequestError("Brave search request failed") from error

        if response.status_code == 429:
            delay_seconds = _retry_after(response)
            await self._brave_keys.defer(lease, delay_seconds)
            raise BraveRateLimited(delay_seconds)

        try:
            response.raise_for_status()
            results = _normalize_search_results(
                response.json(), url_field="url", snippet_field="description"
            )
            if not results:
                raise ProviderRequestError("Brave returned no usable results")
            return results
        except (httpx.HTTPError, ValueError) as error:
            raise ProviderRequestError(
                f"Brave search failed with HTTP {response.status_code}"
            ) from error

    async def _search_duckduckgo(
        self, query: str, max_results: int
    ) -> list[dict[str, str]]:
        await self._duckduckgo_gate.enter()
        try:
            async with self._duckduckgo_client() as client:
                payload = _tool_result_data(
                    await client.call_tool(
                        "search", {"query": query, "max_results": max_results}
                    )
                )
            results = _normalize_duckduckgo_results(payload)
            if not results:
                raise DuckDuckGoUnavailable("DuckDuckGo returned no results")
            return results
        except Exception as error:
            await self._duckduckgo_gate.defer()
            if isinstance(error, DuckDuckGoUnavailable):
                raise
            raise DuckDuckGoUnavailable("DuckDuckGo search request failed") from error

    async def _fetch_duckduckgo(
        self, urls: list[str]
    ) -> tuple[list[dict[str, str]], list[str]]:
        fetched: list[dict[str, str]] = []
        missing: list[str] = []
        async with self._duckduckgo_client() as client:
            outcomes = await asyncio.gather(
                *(client.call_tool("fetch_content", {"url": url}) for url in urls),
                return_exceptions=True,
            )

        for url, outcome in zip(urls, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                missing.append(url)
                continue
            try:
                content = _result_text(_tool_result_data(outcome))
            except ProviderRequestError:
                content = None
            if content:
                fetched.append({"url": url, "content": content})
            else:
                missing.append(url)
        return fetched, missing

    async def _call_tavily(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        if not self._tavily_keys.configured:
            raise ConfigurationError("No Tavily API keys are configured")

        last_error: Exception | None = None
        for _ in range(self._tavily_keys.count):
            api_key = await self._tavily_keys.next_key()
            try:
                async with self._tavily_client_factory(
                    self.config.tavily_endpoint,
                    auth=BearerAuth(api_key),
                    timeout=self.config.request_timeout_seconds,
                ) as client:
                    return _tool_result_data(
                        await client.call_tool(tool_name, arguments)
                    )
            # Remote MCP transports can raise protocol, HTTP, or client errors.
            # Any failed key should advance to the next configured credential.
            except Exception as error:  # noqa: BLE001
                last_error = error

        raise ProviderRequestError(
            "Tavily request failed for every configured key"
        ) from last_error

    async def _search_tavily(
        self, query: str, max_results: int
    ) -> list[dict[str, str]]:
        payload = await self._call_tavily(
            "tavily_search",
            {"query": query, "max_results": max_results, "search_depth": "basic"},
        )
        results = _normalize_search_results(
            payload, url_field="url", snippet_field="content"
        )
        if not results:
            raise ProviderRequestError("Tavily returned no usable results")
        return results

    async def search(
        self,
        query: str,
        max_results: int,
    ) -> list[dict[str, str]]:
        async with self._provider_lock:
            start_index = self._next_provider_index
            self._next_provider_index = (start_index + 1) % len(SEARCH_PROVIDERS)

        providers = SEARCH_PROVIDERS[start_index:] + SEARCH_PROVIDERS[:start_index]
        last_error: Exception | None = None
        for provider in providers:
            try:
                if provider == "duckduckgo":
                    return await self._search_duckduckgo(query, max_results)
                if provider == "brave":
                    return await self._search_brave(
                        query, max_results, wait_for_cooldown=False
                    )
                return await self._search_tavily(query, max_results)
            except (
                BraveCooldown,
                BraveRateLimited,
                ConfigurationError,
                DuckDuckGoUnavailable,
                ProviderRequestError,
            ) as error:
                last_error = error

        raise ProviderRequestError("All web search providers failed") from last_error

    async def fetch_content(self, urls: list[str]) -> list[dict[str, str]]:
        try:
            fetched, missing = await self._fetch_duckduckgo(urls)
        except Exception:  # noqa: BLE001
            fetched, missing = [], urls

        if not missing:
            return fetched

        payload = await self._call_tavily(
            "tavily_extract",
            {
                "urls": missing,
                "extract_depth": "basic",
                "format": "markdown",
                "include_images": False,
            },
        )
        return fetched + _normalize_extracted_content(payload)

    async def research(self, task: str) -> str:
        payload = await self._call_tavily(
            "tavily_research", {"input": task, "model": "auto"}
        )
        return _normalize_research(payload)


mcp = FastMCP(
    "Web Search Router",
    instructions=(
        "Search and fetch web content without selecting a provider. Routing, "
        "fallbacks, cooldowns, and provider parameter translation are automatic."
    ),
)
router: SearchRouter | None = None


@mcp.tool(name="web_search")
async def web_search(
    query: str,
    max_results: int = 10,
) -> list[dict[str, str]]:
    """Search the web. Provider routing and failover are automatic."""
    if not query.strip():
        raise ValueError("query must not be empty")
    if not 1 <= max_results <= 20:
        raise ValueError("max_results must be between 1 and 20")
    if router is None:
        raise RuntimeError("Web search router has not been configured")

    return await router.search(query, max_results)


@mcp.tool(name="fetch_content")
async def fetch_content(urls: list[str]) -> list[dict[str, str]]:
    """Fetch readable Markdown content from one or more web URLs."""
    if router is None:
        raise RuntimeError("Web search router has not been configured")
    return await router.fetch_content(_validate_urls(urls))


@mcp.tool(name="tavily_research")
async def tavily_research(task: str) -> str:
    """Run an in-depth web research task and return the finished report."""
    if not task.strip():
        raise ValueError("task must not be empty")
    if router is None:
        raise RuntimeError("Web search router has not been configured")
    return await router.research(task)


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
