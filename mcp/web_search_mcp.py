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
import uuid
import traceback
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import tomllib
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StdioTransport

# CLIProxyAPI is the configured first-party search backend. Other providers
# remain failover options when its Codex route is unavailable.
SEARCH_PROVIDERS = ("codex_standalone", "brave", "duckduckgo", "tavily")


class ConfigurationError(ValueError):
    pass


class ProviderRequestError(RuntimeError):
    pass


class BraveCooldown(RuntimeError):
    def __init__(self, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        super().__init__("Brave is cooling down")


class DuckDuckGoUnavailable(ProviderRequestError):
    pass


@dataclass(frozen=True)
class RouterConfig:
    duckduckgo_command: str
    duckduckgo_args: tuple[str, ...]
    brave_command: str
    brave_args: tuple[str, ...]
    brave_keys: tuple[str, ...]
    duckduckgo_enabled: bool
    codex_standalone_keys: tuple[str, ...]
    codex_standalone_base_url: str
    codex_standalone_model: str
    tavily_keys: tuple[str, ...]
    tavily_endpoint: str
    brave_cooldown_seconds: float
    duckduckgo_cooldown_seconds: float
    request_timeout_seconds: float


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

    async def acquire(self, wait: bool) -> str:
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
                        return self._keys[index]
                delay_seconds = max(0.0, min(self._available_at) - now)

            if not wait:
                raise BraveCooldown(delay_seconds)
            await asyncio.sleep(delay_seconds)


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


def _boolean(data: dict[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ConfigurationError(f"{key} must be true or false")
    return value


def _http_url(data: dict[str, Any], key: str, default: str) -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ConfigurationError(f"{key} must be an HTTP(S) URL")
    normalized = value.rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigurationError(f"{key} must be an HTTP(S) URL")
    return normalized


def _string(data: dict[str, Any], key: str, default: str) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{key} must be a non-empty string")
    return value.strip()


def _optional_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key, "")
    if not isinstance(value, str):
        raise ConfigurationError(f"{key} must be a string")
    return value.strip()


def _command(
    provider: dict[str, Any],
    provider_name: str,
    default_command: str,
    default_args: list[str],
) -> tuple[str, tuple[str, ...]]:
    command = provider.get("command", default_command)
    args = provider.get("args", default_args)
    if not isinstance(command, str) or not command.strip():
        raise ConfigurationError(f"providers.{provider_name}.command must be a string")
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise ConfigurationError(
            f"providers.{provider_name}.args must be a list of strings"
        )
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
    codex_standalone = providers.get("codex_standalone", {})
    tavily = providers.get("tavily", {})
    if not all(
        isinstance(provider, dict)
        for provider in (duckduckgo, brave, codex_standalone, tavily)
    ):
        raise ConfigurationError("each configured provider must be a table")

    duckduckgo_command, duckduckgo_args = _command(
        duckduckgo,
        "duckduckgo",
        "uvx",
        ["duckduckgo-mcp-server==0.6.1"],
    )
    brave_command, brave_args = _command(
        brave,
        "brave",
        "npx",
        ["-y", "@brave/brave-search-mcp-server@2.1.0"],
    )

    endpoint = tavily.get("endpoint", "https://mcp.tavily.com/mcp/")
    if not isinstance(endpoint, str) or not endpoint.startswith(
        ("https://", "http://")
    ):
        raise ConfigurationError("providers.tavily.endpoint must be an HTTP(S) URL")

    return RouterConfig(
        duckduckgo_command=duckduckgo_command,
        duckduckgo_args=duckduckgo_args,
        brave_command=brave_command,
        brave_args=brave_args,
        brave_keys=_api_keys(brave, "brave"),
        duckduckgo_enabled=_boolean(duckduckgo, "enabled", False),
        codex_standalone_keys=_api_keys(codex_standalone, "codex_standalone"),
        codex_standalone_base_url=_http_url(
            codex_standalone,
            "base_url",
            "https://litellm.v-rail.org/v1",
        ),
        codex_standalone_model=_optional_string(codex_standalone, "model"),
        tavily_keys=_api_keys(tavily, "tavily"),
        tavily_endpoint=endpoint,
        brave_cooldown_seconds=_number(search, "brave_cooldown_seconds", 1.0),
        duckduckgo_cooldown_seconds=_number(
            search, "duckduckgo_cooldown_seconds", 60.0
        ),
        request_timeout_seconds=_number(search, "request_timeout_seconds", 30.0),
    )


def _tool_result_data(result: Any) -> Any:
    if getattr(result, "is_error", False):
        raise ProviderRequestError("Provider returned a tool error")

    for attribute in ("structured_content", "data"):
        value = getattr(result, attribute, None)
        if value is not None:
            return value

    content = getattr(result, "content", None)
    if isinstance(content, list):
        payloads = []
        for item in content:
            text = getattr(item, "text", None)
            if not isinstance(text, str):
                continue
            try:
                payloads.append(json.loads(text))
            except json.JSONDecodeError:
                payloads.append(text)
        if len(payloads) == 1:
            return payloads[0]
        if payloads:
            return payloads
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
        tavily_client_factory: Callable[..., Client] = Client,
        brave_client_factory: Callable[..., Client] = Client,
        duckduckgo_client_factory: Callable[..., Client] = Client,
        codex_client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
    ) -> None:
        self.config = config
        self._brave_keys = BraveKeys(config.brave_keys, config.brave_cooldown_seconds)
        self._codex_standalone_keys = RoundRobinKeys(config.codex_standalone_keys)
        self._tavily_keys = RoundRobinKeys(config.tavily_keys)
        self._duckduckgo_gate = CooldownGate(config.duckduckgo_cooldown_seconds)
        self._next_provider_index = 0
        self._provider_lock = asyncio.Lock()
        self._tavily_client_factory = tavily_client_factory
        self._brave_client_factory = brave_client_factory
        self._duckduckgo_client_factory = duckduckgo_client_factory
        self._codex_client_factory = codex_client_factory
        self.session_id = str(uuid.uuid4())
        self._url_to_view_ref: dict[str, str] = {}

    def record_view_ref(self, ref_id: str, output: str) -> None:
        m = re.search(r"cite(turn\d+view\d+)", output)
        if m:
            view_ref = m.group(1)
            self._url_to_view_ref[ref_id] = view_ref
            self._url_to_view_ref[view_ref] = view_ref

    def resolve_view_ref(self, ref_id: str) -> str:
        return self._url_to_view_ref.get(ref_id, ref_id)

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
        api_key = await self._brave_keys.acquire(wait=wait_for_cooldown)
        transport = StdioTransport(
            command=self.config.brave_command,
            args=list(self.config.brave_args),
            env={
                "BRAVE_API_KEY": api_key,
                "BRAVE_MCP_ENABLED_TOOLS": "brave_web_search",
            },
            keep_alive=False,
        )
        try:
            async with self._brave_client_factory(
                transport, timeout=self.config.request_timeout_seconds
            ) as client:
                payload = _tool_result_data(
                    await client.call_tool(
                        "brave_web_search",
                        {"query": query, "count": max_results},
                    )
                )
            results = _normalize_search_results(
                payload, url_field="url", snippet_field="description"
            )
            if not results and not (isinstance(payload, dict) and (payload.get("results") == [] or isinstance(payload.get("web"), dict) and payload["web"].get("results") == [])):
                raise ProviderRequestError("Brave returned no usable results")
            return results
        except Exception as error:
            if isinstance(error, ProviderRequestError):
                raise
            raise ProviderRequestError("Brave MCP search failed") from error

    async def _search_duckduckgo(
        self, query: str, max_results: int
    ) -> list[dict[str, str]]:
        if not self.config.duckduckgo_enabled:
            raise ConfigurationError("DuckDuckGo is disabled")
        await self._duckduckgo_gate.enter()
        try:
            async with self._duckduckgo_client() as client:
                payload = _tool_result_data(
                    await client.call_tool(
                        "search", {"query": query, "max_results": max_results}
                    )
                )
            results = _normalize_duckduckgo_results(payload)
            return results
        except Exception as error:
            await self._duckduckgo_gate.defer()
            if isinstance(error, DuckDuckGoUnavailable):
                raise
            raise DuckDuckGoUnavailable("DuckDuckGo search request failed") from error

    async def _search_codex_standalone(
        self, query: str, max_results: int
    ) -> list[dict[str, str]]:
        if not self._codex_standalone_keys.configured:
            raise ConfigurationError("No Codex standalone API keys are configured")

        endpoint = f"{self.config.codex_standalone_base_url.rstrip('/')}/alpha/search"
        last_error: Exception | None = None
        for _ in range(self._codex_standalone_keys.count):
            api_key = await self._codex_standalone_keys.next_key()
            try:
                async with self._codex_client_factory(
                    timeout=self.config.request_timeout_seconds
                ) as client:
                    response = await client.post(
                        endpoint,
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                        },
                        json={
                            "id": str(uuid.uuid4()),
                            "model": self.config.codex_standalone_model,
                            "commands": {"search_query": [{"q": query}]},
                        },
                    )
                    response.raise_for_status()
                    payload = response.json()
                results = _normalize_search_results(
                    payload, url_field="url", snippet_field="snippet"
                )[:max_results]
                if results:
                    return results
                if isinstance(payload, dict) and payload.get("results") == []:
                    return []
                raise ProviderRequestError(
                    "Codex standalone search returned no usable results"
                )
            except Exception as error:  # noqa: BLE001
                last_error = error

        raise ProviderRequestError(
            "Codex standalone search failed for every configured key"
        ) from last_error
    async def _fetch_single_codex(
        self, url: str, client: httpx.AsyncClient, api_key: str
    ) -> str | None:
        endpoint = f"{self.config.codex_standalone_base_url.rstrip('/')}/alpha/search"
        try:
            response = await client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                },
                json={
                    "id": str(uuid.uuid4()),
                    "model": self.config.codex_standalone_model,
                    "commands": {"open": [{"ref_id": url}]},
                },
            )
            response.raise_for_status()
            payload = response.json()
            raw_output = payload.get("output", "")
            if not raw_output:
                return None
            lines = []
            for line in raw_output.splitlines():
                if line.startswith("L") and ":" in line[:6]:
                    lines.append(line.split(":", 1)[1].strip())
                elif not line.startswith("cite") and not line.startswith("http") and not line.startswith("Total lines:"):
                    lines.append(line)
            content = "\n".join(lines).strip()
            return content or None
        except Exception:  # noqa: BLE001
            return None

    async def _fetch_codex_standalone(
        self, urls: list[str]
    ) -> tuple[list[dict[str, str]], list[str]]:
        if not self._codex_standalone_keys.configured:
            return [], urls
        api_key = await self._codex_standalone_keys.next_key()
        async with self._codex_client_factory(
            timeout=self.config.request_timeout_seconds
        ) as client:
            tasks = [self._fetch_single_codex(url, client, api_key) for url in urls]
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)

        fetched: list[dict[str, str]] = []
        missing: list[str] = []
        for url, outcome in zip(urls, outcomes, strict=True):
            if isinstance(outcome, str) and outcome:
                fetched.append({"url": url, "content": outcome})
            else:
                missing.append(url)
        return fetched, missing


    async def _fetch_duckduckgo(
        self, urls: list[str]
    ) -> tuple[list[dict[str, str]], list[str]]:
        if not self.config.duckduckgo_enabled:
            raise ConfigurationError("DuckDuckGo is disabled")
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
        if not results and not (isinstance(payload, dict) and payload.get("results") == []):
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
        failures: list[str] = []
        for provider in providers:
            try:
                if provider == "duckduckgo":
                    results = await self._search_duckduckgo(query, max_results)
                elif provider == "brave":
                    results = await self._search_brave(query, max_results, wait_for_cooldown=False)
                elif provider == "codex_standalone":
                    results = await self._search_codex_standalone(query, max_results)
                else:
                    results = await self._search_tavily(query, max_results)
                if results:
                    return results
            except (
                BraveCooldown,
                ConfigurationError,
                DuckDuckGoUnavailable,
                ProviderRequestError,
            ) as error:
                detail = "".join(traceback.format_exception(type(error), error, error.__traceback__))
                for secret in (*self.config.brave_keys, *self.config.codex_standalone_keys, *self.config.tavily_keys):
                    if secret:
                        detail = detail.replace(secret, "[REDACTED]")
                detail = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[REDACTED]", detail)
                detail = re.sub(r"(?i)((?:api[_-]?key|token|key)=)[^&\s\"']+", r"\1[REDACTED]", detail)
                failures.append(f"[{provider}]\n{detail}")
        if failures:
            raise ProviderRequestError("Search exhausted without results; provider errors (credentials redacted):\n" + "\n".join(failures)) from None
        return []

    async def fetch_content(self, urls: list[str]) -> list[dict[str, str]]:
        """Fetch page content through Codex native open command, with DuckDuckGo failover."""
        fetched_all: dict[str, str] = {}
        remaining_urls = list(urls)

        # 1. Try Codex standalone first if configured
        if self._codex_standalone_keys.configured:
            try:
                codex_fetched, codex_missing = await self._fetch_codex_standalone(remaining_urls)
                for item in codex_fetched:
                    fetched_all[item["url"]] = item["content"]
                remaining_urls = codex_missing
            except Exception:  # noqa: BLE001
                pass

        # 2. Fall back to DuckDuckGo for any remaining URLs if enabled
        if remaining_urls and self.config.duckduckgo_enabled:
            try:
                ddg_fetched, ddg_missing = await self._fetch_duckduckgo(remaining_urls)
                for item in ddg_fetched:
                    fetched_all[item["url"]] = item["content"]
                remaining_urls = ddg_missing
            except Exception:  # noqa: BLE001
                pass

        if not fetched_all:
            raise ProviderRequestError(
                "Could not fetch any requested URLs using available providers"
            )

        # Return in original requested order for successfully fetched URLs
        return [{"url": u, "content": fetched_all[u]} for u in urls if u in fetched_all]

    async def research(self, task: str) -> str:
        payload = await self._call_tavily(
            "tavily_research", {"input": task, "model": "auto"}
        )
        return _normalize_research(payload)
    async def execute_codex_command(
        self, command_name: str, command_payload: list[dict[str, Any]]
    ) -> dict[str, Any]:
        if not self._codex_standalone_keys.configured:
            raise ConfigurationError("Codex standalone is not configured")
        api_key = await self._codex_standalone_keys.next_key()
        endpoint = f"{self.config.codex_standalone_base_url.rstrip('/')}/alpha/search"
        async with self._codex_client_factory(
            timeout=self.config.request_timeout_seconds
        ) as client:
            response = await client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
                },
                json={
                    "id": self.session_id,
                    "model": self.config.codex_standalone_model,
                    "commands": {command_name: command_payload},
                },
            )
            response.raise_for_status()
            return response.json()



mcp = FastMCP(
    "Web Search Router",
    instructions=(
        "REQUIRED for current or externally verifiable information: search the live internet "
        "for benchmark scores, specifications, prices, news, documentation, and comparisons. "
        "Use this instead of model memory. Return source URLs, prefer primary or reputable "
        "sources, distinguish measured results from estimates, and use automatic provider "
        "routing and failover. This is the single entry point; do not use provider-specific "
        "MCPs directly."
    ),
)
router: SearchRouter | None = None
def register_tools(app: FastMCP, cfg: RouterConfig) -> None:
    @app.tool(name="web-search")
    async def web_search(
        query: str,
        max_results: int = 10,
    ) -> list[dict[str, str]] | str:
        "REQUIRED live-web search for current or externally verifiable information, including benchmark scores, specifications, prices, news, documentation, and comparisons. Prefer this over model memory; return source URLs, distinguish measured results from estimates, and use automatic provider routing and failover. This is the single entry point; do not use provider-specific MCPs directly."
        if not query.strip():
            raise ValueError("query must not be empty")
        if not 1 <= max_results <= 20:
            raise ValueError("max_results must be between 1 and 20")
        if router is None:
            raise RuntimeError("Web search router has not been configured")
        results = await router.search(query, max_results)
        return results if results else "No results found."

    if cfg.codex_standalone_keys or cfg.duckduckgo_enabled:
        @app.tool(name="fetch")
        async def fetch_content(
            urls: list[str] | None = None,
            url: str | None = None,
        ) -> list[dict[str, str]]:
            """Fetch readable page content for given URLs using Codex native page extraction, with automatic DuckDuckGo failover.

            REQUIRED parameter: `urls` (list of URLs, e.g. ["https://example.com"]) or `url` (single string URL).
            """
            if router is None:
                raise RuntimeError("Web search router has not been configured")
            resolved_urls: list[str] = []
            if urls:
                resolved_urls.extend(urls)
            if url:
                resolved_urls.append(url)

            if not resolved_urls:
                raise ValueError("fetch requires either 'urls' (list of URLs) or 'url' (single URL string)")

            return await router.fetch_content(_validate_urls(resolved_urls))
    if cfg.codex_standalone_keys:
        @app.tool(name="open-page")
        async def open_page(ref_id: str, lineno: int | None = None) -> str:
            """Open the page indicated by `ref_id` or URL and position viewport at line `lineno`."""
            if router is None:
                raise RuntimeError("Web search router has not been configured")
            res = await router.execute_codex_command("open", [{"ref_id": ref_id, "lineno": lineno}])
            output = res.get("output", "")
            router.record_view_ref(ref_id, output)
            return output

        @app.tool(name="click-link")
        async def click_link(ref_id: str, link_id: int) -> str:
            """Open the link `link_id` (numbered link `【{id}†.*】`) from previously opened page `ref_id`."""
            if router is None:
                raise RuntimeError("Web search router has not been configured")
            resolved_ref = router.resolve_view_ref(ref_id)
            res = await router.execute_codex_command("click", [{"ref_id": resolved_ref, "id": link_id}])
            output = res.get("output", "")
            router.record_view_ref(ref_id, output)
            router.record_view_ref(resolved_ref, output)
            return output

        @app.tool(name="find-in-page")
        async def find_in_page(ref_id: str, pattern: str) -> str:
            """Find text pattern in page indicated by `ref_id` or URL."""
            if router is None:
                raise RuntimeError("Web search router has not been configured")
            resolved_ref = router.resolve_view_ref(ref_id)
            res = await router.execute_codex_command("find", [{"ref_id": resolved_ref, "pattern": pattern}])
            output = res.get("output", "")
            router.record_view_ref(ref_id, output)
            router.record_view_ref(resolved_ref, output)
            return output

        @app.tool(name="image-search")
        async def image_search(query: str, recency_days: int | None = None, domains: list[str] | None = None) -> list[dict[str, Any]]:
            """Query image search engine for a given query."""
            if router is None:
                raise RuntimeError("Web search router has not been configured")
            payload: dict[str, Any] = {"q": query}
            if recency_days is not None:
                payload["recency"] = recency_days
            if domains is not None:
                payload["domains"] = domains
            res = await router.execute_codex_command("image_query", [payload])
            raw_output = res.get("output", "")
            if not raw_output:
                return res.get("results", [])
            results: list[dict[str, Any]] = []
            for block in re.split(r"-{20,}", raw_output):
                block = block.strip()
                if not block:
                    continue
                lines = block.splitlines()
                first_line = lines[0] if lines else ""
                title = first_line
                page_url = ""
                m_page = re.match(r"^(.*?)\s*\((https?://[^\s)]+)\)$", first_line)
                if m_page:
                    title = m_page.group(1).strip()
                    page_url = m_page.group(2).strip()
                image_url = ""
                m_img = re.search(r"Image URL:\s*(https?://[^\s#]+)", block)
                if m_img:
                    image_url = m_img.group(1)
                desc_lines = [
                    line.strip()
                    for line in lines[1:]
                    if not line.startswith("Image URL:")
                    and not line.startswith("\ue200cite")
                    and line.strip()
                ]
                results.append({
                    "title": title,
                    "page_url": page_url,
                    "image_url": image_url,
                    "description": "\n".join(desc_lines),
                })
            return results or res.get("results", [])

        @app.tool(name="finance")
        async def finance(ticker: str, asset_type: str = "equity", market: str | None = None) -> str:
            """Look up financial quotes for a given ticker (type: equity, fund, crypto, index)."""
            if router is None:
                raise RuntimeError("Web search router has not been configured")
            payload: dict[str, Any] = {"ticker": ticker, "type": asset_type}
            if market:
                payload["market"] = market
            res = await router.execute_codex_command("finance", [payload])
            return res.get("output", "")

        @app.tool(name="weather")
        async def weather(location: str, start_date: str | None = None, duration_days: int | None = None) -> str:
            """Look up weather forecast for location (e.g. 'City, Country')."""
            if router is None:
                raise RuntimeError("Web search router has not been configured")
            payload: dict[str, Any] = {"location": location}
            if start_date:
                payload["start"] = start_date
            if duration_days:
                payload["duration"] = duration_days
            res = await router.execute_codex_command("weather", [payload])
            return res.get("output", "")

        @app.tool(name="sports")
        async def sports(league: str, fn: str = "schedule", team: str | None = None) -> str:
            """Look up sports schedules and standings (league: nba, wnba, nfl, nhl, mlb, epl, ncaamb, ncaawb, ipl; fn: schedule or standings)."""
            if router is None:
                raise RuntimeError("Web search router has not been configured")
            payload: dict[str, Any] = {"tool": "sports", "fn": fn, "league": league}
            if team:
                payload["team"] = team
            res = await router.execute_codex_command("sports", [payload])
            return res.get("output", "")

        @app.tool(name="world-time")
        async def world_time(utc_offset: str) -> str:
            """Get current time for UTC offset (e.g. '+08:00' or '-05:00')."""
            if router is None:
                raise RuntimeError("Web search router has not been configured")
            res = await router.execute_codex_command("time", [{"utc_offset": utc_offset}])
            return res.get("output", "")

    if cfg.tavily_keys:
        @app.tool(name="research")
        async def tavily_research(task: str) -> str:
            """Run deeper live-web research when a question needs multiple sources, corroboration, or synthesis. Return source URLs and distinguish measured results from estimates."""
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
            f"(Brave keys: {len(config.brave_keys)}, "
            f"DuckDuckGo: {'enabled' if config.duckduckgo_enabled else 'disabled'}, "
            f"Codex standalone keys: {len(config.codex_standalone_keys)}, "
            f"Tavily keys: {len(config.tavily_keys)})"
        )
        return

    global router
    router = SearchRouter(config)
    register_tools(mcp, config)
    mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
