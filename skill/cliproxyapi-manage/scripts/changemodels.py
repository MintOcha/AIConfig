#!/usr/bin/env python3
"""Discover and manage OpenAI-compatible model providers in CLIProxyAPI."""

import argparse
from getpass import getpass
import json
from pathlib import Path
import re
import sys
import tempfile
from urllib.request import Request, urlopen

import yaml


def normalize_url(value: object) -> str:
    return str(value).strip().rstrip("/")


def normalize_prefix(value: object) -> str:
    return str(value).strip().strip("/")


def fetch_models(base_url: str, api_key: str) -> list[str]:
    models_url = f"{normalize_url(base_url)}/models"
    headers = {
        "Accept": "application/json",
        "User-Agent": "CLIProxyAPI-model-configurator/1.0",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(
        models_url,
        headers=headers,
    )
    with urlopen(request) as response:
        payload = yaml.safe_load(response.read())

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError(f"{models_url} returned an unexpected model response")
    models = sorted(
        {
            item["id"].strip()
            for item in payload["data"]
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and item["id"].strip()
        }
    )
    if not models:
        raise ValueError(f"{models_url} returned no model IDs")
    return models


def write_config(config_path: Path, content: str) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=config_path.parent, delete=False
    ) as temp_file:
        temp_path = Path(temp_file.name)
        temp_file.write(content)
    temp_path.chmod(config_path.stat().st_mode)
    temp_path.replace(config_path)


def yaml_string(value: str) -> str:
    return json.dumps(value)


def openai_compatibility_bounds(lines: list[str]) -> tuple[int, int]:
    start = next(
        (index for index, line in enumerate(lines) if line.strip() == "openai-compatibility:"),
        -1,
    )
    if start < 0:
        raise ValueError("openai-compatibility is missing from the configuration")
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index] and not lines[index][0].isspace() and not lines[index].startswith("#")
        ),
        len(lines),
    )
    return start, end


def provider_bounds(
    lines: list[str], start: int, end: int, prefix: str
) -> list[tuple[int, int]]:
    matches = []
    starts = [index for index in range(start + 1, end) if lines[index].startswith("  - ")]
    for index, provider_start in enumerate(starts):
        provider_end = starts[index + 1] if index + 1 < len(starts) else end
        for line in lines[provider_start:provider_end]:
            match = re.match(r'^  prefix:\s*["\']?([^"\'\s]+)', line)
            if match and normalize_prefix(match.group(1)) == prefix:
                matches.append((provider_start, provider_end))
                break
    return matches


def provider_config(prefix: str, base_url: str, api_key: str, model_ids: list[str]) -> list[str]:
    lines = [
        f"- name: {yaml_string(prefix)}\n",
        f"  prefix: {yaml_string(prefix)}\n",
        f"  base-url: {yaml_string(normalize_url(base_url))}\n",
    ]
    if api_key:
        lines.extend(
            [
                "  api-key-entries:\n",
                f"  - api-key: {yaml_string(api_key)}\n",
            ]
        )
    lines.append("  models:\n")
    for model_id in model_ids:
        lines.extend(
            [
                f"  - name: {yaml_string(model_id)}\n",
                f"    alias: {yaml_string(model_id)}\n",
            ]
        )
    return lines


def sync_provider(
    config_path: Path,
    base_url: str,
    prefix: str,
    model_ids: list[str],
    api_key: str,
) -> None:
    prefix = normalize_prefix(prefix)
    if not prefix:
        raise ValueError("Provider prefix cannot be empty")
    lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
    start, end = openai_compatibility_bounds(lines)
    replacement = provider_config(prefix, base_url, api_key, model_ids)
    existing = provider_bounds(lines, start, end, prefix)
    if not existing:
        lines[end:end] = replacement
    else:
        for duplicate_start, duplicate_end in reversed(existing[1:]):
            del lines[duplicate_start:duplicate_end]
        lines[existing[0][0]:existing[0][1]] = replacement
    write_config(config_path, "".join(lines))


def delete_provider(config_path: Path, prefix: str) -> bool:
    prefix = normalize_prefix(prefix)
    if not prefix:
        raise ValueError("Provider prefix cannot be empty")
    lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
    start, end = openai_compatibility_bounds(lines)
    existing = provider_bounds(lines, start, end, prefix)
    if not existing:
        return False
    for provider_start, provider_end in reversed(existing):
        del lines[provider_start:provider_end]
    write_config(config_path, "".join(lines))
    return True


def prompt_value(label: str) -> str:
    try:
        value = input(f"{label}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(0) from None
    if not value:
        raise ValueError(f"{label} cannot be empty")
    return value


def prompt_api_key() -> str:
    try:
        return getpass("API key (leave blank when none): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(0) from None


def interactive_action() -> tuple[str, str, str, str, str]:
    print("Custom model provider")
    print("  1. Add or sync provider")
    print("  2. Delete provider")
    action = prompt_value("Choose an action")
    if action == "1":
        return (
            action,
            prompt_value("Model-discovery endpoint"),
            prompt_value("OpenAI-compatible endpoint"),
            prompt_value("Provider prefix"),
            prompt_api_key(),
        )
    if action == "2":
        return action, "", "", prompt_value("Enter the provider prefix"), ""
    raise ValueError("Choose 1 to add or sync, or 2 to delete")


def resolve_default_config() -> Path:
    candidates = [
        Path.cwd() / "config.yaml",
        Path("/home/nas/Projects/CLIProxyAPI/config.yaml"),
        Path(__file__).resolve().parent / "config.yaml",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return candidates[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=resolve_default_config())
    args = parser.parse_args()
    config_path = args.config.resolve()
    if not config_path.is_file():
        print(f"Config file not found: {config_path}", file=sys.stderr)
        return 1
    try:
        action, discovery_url, base_url, prefix, api_key = interactive_action()
        if action == "2":
            if delete_provider(config_path, prefix):
                print(f"Deleted provider prefix {normalize_prefix(prefix)}/.")
            else:
                print(f"No provider found with prefix {normalize_prefix(prefix)}/.")
            return 0
        model_ids = fetch_models(discovery_url, api_key)
        sync_provider(config_path, base_url, prefix, model_ids, api_key)
        print(
            f"Configured {normalize_prefix(prefix)}/ with {len(model_ids)} models "
            f"from {normalize_url(base_url)}."
        )
    except (OSError, ValueError) as error:
        print(f"Configuration failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
