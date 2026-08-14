#!/usr/bin/env python3
import argparse
import json
import os
import sys
import uuid
import urllib.request
import urllib.error

BASE_URL = "https://litellm.v-rail.org"
MODELS_ENDPOINT = f"{BASE_URL}/v1/models"
STANDALONE_SEARCH_ENDPOINT = f"{BASE_URL}/v1/alpha/search"
HOME = os.path.expanduser("~")


class AuthError(Exception):
    pass


def fetch_models(api_key):
    req = urllib.request.Request(MODELS_ENDPOINT)
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("User-Agent", "setup-agent/1.0")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AuthError(f"HTTP {e.code}")
        print(f"Error fetching models: HTTP {e.code}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Error fetching models: {e.reason}", file=sys.stderr)
        sys.exit(1)
    model_ids = sorted(m["id"] for m in data.get("data", []))
    if not model_ids:
        print("No models found at endpoint.", file=sys.stderr)
        sys.exit(1)
    return model_ids


def standalone_search_model(models):
    preferred = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.4")
    for model in preferred:
        if model in models:
            return model
    return next(
        (model for model in models if model.lower().startswith("gpt-")),
        None,
    )


def supports_standalone_web_search(api_key, models):
    """Return whether v-rail can execute Codex's standalone search contract."""
    model = standalone_search_model(models)
    if model is None:
        return False

    payload = json.dumps({
        "id": f"setup-agent-{uuid.uuid4()}",
        "model": model,
        "commands": {"search_query": [{"q": "OpenAI"}]},
    }).encode()
    req = urllib.request.Request(
        STANDALONE_SEARCH_ENDPOINT,
        data=payload,
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "setup-agent/1.0")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            json.loads(resp.read().decode())
            return resp.status == 200
    except (OSError, ValueError, urllib.error.HTTPError):
        return False


def detect_standalone_web_search(api_key, models):
    print("  Checking for native Codex standalone web search...")
    if not supports_standalone_web_search(api_key, models):
        print("  Standalone web search is not available at this endpoint; skipped.")
        return False

    print("  Standalone web search is supported by the endpoint; enabling it.")
    return True


def find_existing_api_key():
    """Try to discover an existing v-rail API key from known agent configs."""
    # Claude Code
    claude_path = os.path.join(HOME, ".claude", "settings.json")
    data = read_json(claude_path)
    if data.get("env", {}).get("ANTHROPIC_BASE_URL") == BASE_URL:
        token = data.get("env", {}).get("ANTHROPIC_AUTH_TOKEN")
        if token:
            return token, claude_path

    # Factory Droid
    droid_path = os.path.join(HOME, ".factory", "settings.json")
    data = read_json(droid_path)
    for m in data.get("customModels", []):
        base = m.get("baseUrl", "")
        if base.startswith(BASE_URL) and m.get("apiKey"):
            return m["apiKey"], droid_path

    # OpenCode
    oc_path = os.path.join(HOME, ".config", "opencode", "opencode.json")
    data = read_json(oc_path)
    for prov in data.get("provider", {}).values():
        opts = prov.get("options", {})
        if str(opts.get("baseURL", "")).startswith(BASE_URL) and opts.get("apiKey"):
            return opts["apiKey"], oc_path

    # KiloCode
    kilo_auth = os.path.join(HOME, ".local", "share", "kilo", "auth.json")
    data = read_json(kilo_auth)
    entry = data.get("cliproxyapi", {})
    if entry.get("key"):
        return entry["key"], kilo_auth

    # Codex
    codex_auth = os.path.join(HOME, ".codex", "auth.json")
    data = read_json(codex_auth)
    if data.get("OPENAI_API_KEY"):
        return data["OPENAI_API_KEY"], codex_auth

    return None, None


def prompt_api_key():
    try:
        key = input("Enter API key for v-rail (litellm.v-rail.org): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)
    if not key:
        print("No API key provided.", file=sys.stderr)
        sys.exit(1)
    return key


def resolve_api_key(cli_key):
    """Resolve a working API key and return (api_key, models)."""
    # 1. Explicit --api flag
    if cli_key:
        try:
            return cli_key, fetch_models(cli_key)
        except AuthError:
            print("Provided API key failed to authenticate with v-rail.", file=sys.stderr)
            sys.exit(1)

    # 2. Existing key from config
    existing, src = find_existing_api_key()
    if existing:
        print(f"Using existing API key from {src}")
        try:
            return existing, fetch_models(existing)
        except AuthError:
            print("Existing API key could not authenticate with v-rail.")

    # 3. Interactive prompt (retry until it works)
    while True:
        key = prompt_api_key()
        try:
            return key, fetch_models(key)
        except AuthError:
            print("That API key failed to authenticate with v-rail. Try again.")


def read_json(path):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    print(f"  Written: {path}")


def print_model_list(models):
    print("\n  Available models:")
    for i, m in enumerate(models, 1):
        print(f"    {i:>3}. {m}")
    print()


def confirm_overwrite(agents):
    print("WARNING: This will COMPLETELY REPLACE existing model configuration for:")
    for a in agents:
        print(f"  - {a}")
    print("Any previously configured custom models from other providers will be lost for those agents.")
    print("Other settings (permissions, plugins, projects, etc.) will be preserved.")
    print()
    try:
        answer = input("Proceed? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    if answer not in ("y", "yes"):
        print("Aborted.")
        sys.exit(0)


def setup_claude(api_key, models):
    print("\n[Claude Code] Setting up base URL, auth token, and model discovery...")
    path = os.path.join(HOME, ".claude", "settings.json")
    data = read_json(path)
    data.setdefault("env", {})
    data["env"]["ANTHROPIC_BASE_URL"] = BASE_URL
    data["env"]["ANTHROPIC_AUTH_TOKEN"] = api_key
    data["env"]["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] = "1"
    data["env"].pop("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", None)
    data.setdefault("permissions", {})
    data["permissions"]["defaultMode"] = "bypassPermissions"
    write_json(path, data)
    print(f"  Enabled gateway model discovery and bypass permissions mode. {len(models)} models added.")


def setup_droid(api_key, models):
    print("\n[Factory Droid] Replacing custom models with full sync...")
    path = os.path.join(HOME, ".factory", "settings.json")
    data = read_json(path)
    data["customModels"] = [
        {
            "model": mid,
            "id": f"custom:v-rail:-{mid}-{i}",
            "index": i,
            "baseUrl": BASE_URL if "claude" in mid.lower() else f"{BASE_URL}/v1",
            "apiKey": api_key,
            "displayName": f"v-rail: {mid}",
            "maxOutputTokens": 65536,
            "noImageSupport": False,
            "provider": "anthropic" if "claude" in mid.lower() else "openai",
        }
        for i, mid in enumerate(models)
    ]
    write_json(path, data)


def setup_opencode(api_key, models):
    print("\n[OpenCode] Replacing provider models with full sync...")
    path = os.path.join(HOME, ".config", "opencode", "opencode.json")
    data = read_json(path)
    data.setdefault("provider", {})
    data["provider"]["cliproxyapi"] = {
        "npm": "@ai-sdk/openai-compatible",
        "name": "CLIProxyAPI v-rail",
        "options": {
            "baseURL": f"{BASE_URL}/v1",
            "apiKey": api_key,
        },
        "models": {mid: {"name": mid} for mid in models},
    }
    data["model"] = f"cliproxyapi/{models[0]}"
    data["small_model"] = f"cliproxyapi/{models[0]}"
    write_json(path, data)


def setup_kilocode(api_key, models):
    print("\n[KiloCode] Replacing provider with full sync...")

    # Auth
    auth_path = os.path.join(HOME, ".local", "share", "kilo", "auth.json")
    auth_data = read_json(auth_path)
    auth_data["cliproxyapi"] = {"type": "api", "key": api_key}
    write_json(auth_path, auth_data)

    # Model selection
    model_path = os.path.join(HOME, ".local", "state", "kilo", "model.json")
    model_data = read_json(model_path)
    model_data.setdefault("model", {})
    model_data["model"]["code"] = {
        "providerID": "cliproxyapi",
        "modelID": models[0],
    }
    model_data["recent"] = [
        {"providerID": "cliproxyapi", "modelID": m} for m in models[:20]
    ]
    write_json(model_path, model_data)

    # Inject into cached models registry
    cache_path = os.path.join(HOME, ".cache", "kilo", "models.json")
    if os.path.exists(cache_path):
        cache_data = read_json(cache_path)
        cache_data["cliproxyapi"] = {
            "id": "cliproxyapi",
            "npm": "@ai-sdk/openai-compatible",
            "api": f"{BASE_URL}/v1",
            "name": "CLIProxyAPI v-rail",
            "models": {
                mid: {
                    "id": mid,
                    "name": mid,
                    "family": "v-rail",
                    "attachment": True,
                    "reasoning": False,
                    "tool_call": True,
                    "modalities": {"input": ["text"], "output": ["text"]},
                    "limit": {"context": 131072, "output": 65536},
                }
                for mid in models
            },
        }
        write_json(cache_path, cache_data)
    else:
        print(f"  Skipped cache (not found): {cache_path}")


def setup_codex(api_key, models):
    print("\n[Codex] Setting up config...")
    enable_standalone_search = detect_standalone_web_search(api_key, models)
    auth_path = os.path.join(HOME, ".codex", "auth.json")
    auth_data = read_json(auth_path)
    auth_data["auth_mode"] = "apikey"
    auth_data["OPENAI_API_KEY"] = api_key
    write_json(auth_path, auth_data)

    config_path = os.path.join(HOME, ".codex", "config.toml")
    config_lines = []
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            config_lines = f.readlines()

    new_lines = []
    section = None
    skip_managed_section = False
    for line in config_lines:
        stripped = line.strip()

        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped
            skip_managed_section = section == "[model_providers.v-rail]"
            if skip_managed_section:
                continue

        if skip_managed_section:
            continue

        key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
        if section is None and key in ("model_provider", "base_url"):
            continue
        if (
            section == "[tui.model_availability_nux]"
            and key == "base_url"
            and f'{BASE_URL}/v1' in stripped
        ):
            continue

        new_lines.append(line)

    first_section = next(
        (i for i, line in enumerate(new_lines) if line.lstrip().startswith("[")),
        len(new_lines),
    )
    new_lines.insert(first_section, 'model_provider = "v-rail"\n')

    while new_lines and not new_lines[-1].strip():
        new_lines.pop()
    new_lines.extend([
        "\n\n[model_providers.v-rail]\n",
        'name = "v-rail"\n',
        f'base_url = "{BASE_URL}/v1"\n',
        'wire_api = "responses"\n',
        "requires_openai_auth = true\n",
    ])
    if enable_standalone_search:
        new_lines.append("supports_standalone_web_search = true\n")

    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    with open(config_path, "w") as f:
        f.writelines(new_lines)
    print(f"  Written: {config_path}")
    if enable_standalone_search:
        print("  Enabled native standalone web search using the v-rail endpoint.")

    print("  Codex does not support custom model lists via config.")
    print("  Set the model manually in config.toml. Available models:")
    print_model_list(models)


AGENT_NAMES = {
    "claude": "Claude Code (~/.claude/)",
    "droid": "Factory Droid (~/.factory/)",
    "opencode": "OpenCode (~/.config/opencode/)",
    "kilocode": "KiloCode (~/.local/share/kilo/, ~/.cache/kilo/)",
    "codex": "Codex (~/.codex/)",
}


def main():
    parser = argparse.ArgumentParser(
        description="Setup AI agent harness configs for litellm.v-rail.org"
    )
    parser.add_argument(
        "--api", "--api-key", "--key",
        dest="api_key",
        default=None,
        help="API key for litellm.v-rail.org. If omitted, the existing key "
             "from config is used, falling back to an interactive prompt.",
    )
    parser.add_argument("--claude", action="store_true", help="Setup Claude Code")
    parser.add_argument(
        "--droid", "--factory", dest="droid", action="store_true",
        help="Setup Factory Droid",
    )
    parser.add_argument("--opencode", action="store_true", help="Setup OpenCode")
    parser.add_argument(
        "--kilo", "--kilocode", dest="kilocode", action="store_true",
        help="Setup KiloCode",
    )
    parser.add_argument("--codex", action="store_true", help="Setup Codex")

    args = parser.parse_args()

    selected = []
    if args.claude:
        selected.append("claude")
    if args.droid:
        selected.append("droid")
    if args.opencode:
        selected.append("opencode")
    if args.kilocode:
        selected.append("kilocode")
    if args.codex:
        selected.append("codex")

    if not selected:
        parser.error(
            "Specify at least one agent: --claude, --droid/--factory, "
            "--opencode, --kilo/--kilocode, --codex"
        )

    print(f"Fetching models from {MODELS_ENDPOINT}...")
    api_key, models = resolve_api_key(args.api_key)
    print(f"Found {len(models)} models.")

    # Agents that do full overwrite of model lists
    overwrite_agents = [a for a in selected if a in ("droid", "opencode", "kilocode")]
    if overwrite_agents:
        confirm_overwrite([AGENT_NAMES[a] for a in overwrite_agents])

    if "claude" in selected:
        setup_claude(api_key, models)
    if "droid" in selected:
        setup_droid(api_key, models)
    if "opencode" in selected:
        setup_opencode(api_key, models)
    if "kilocode" in selected:
        setup_kilocode(api_key, models)
    if "codex" in selected:
        setup_codex(api_key, models)

    print("\nDone.")


if __name__ == "__main__":
    main()
