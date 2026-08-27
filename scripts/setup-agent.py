#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
import urllib.request
import urllib.error
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

BASE_URL = "https://litellm.v-rail.org"
MODELS_ENDPOINT = f"{BASE_URL}/v1/models"
STANDALONE_SEARCH_ENDPOINT = f"{BASE_URL}/v1/alpha/search"
HOME = os.path.expanduser("~")
REPO_ROOT = Path(__file__).resolve().parents[1]


class AuthError(Exception):
    pass


def fetch_models(api_key, endpoint=MODELS_ENDPOINT):
    req = urllib.request.Request(endpoint)
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("User-Agent", "setup-agent/1.0")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AuthError(f"HTTP {e.code}")
        print(f"Error fetching models from {endpoint}: HTTP {e.code}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Error fetching models from {endpoint}: {e.reason}", file=sys.stderr)
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

    # Oh My Pi (omp)
    omp_path = os.path.join(HOME, ".omp", "agent", "models.yml")
    data = read_yaml(omp_path)
    for prov in data.get("providers", {}).values():
        if str(prov.get("baseUrl", "")).startswith(BASE_URL) and prov.get("apiKey"):
            return prov["apiKey"], omp_path

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


def read_yaml(path):
    if not os.path.exists(path) or yaml is None:
        return {}
    try:
        with open(path, "r") as f:
            return yaml.safe_load(f) or {}
    except yaml.YAMLError:
        return {}


def write_yaml(path, data):
    if yaml is None:
        print("Error: PyYAML is required for omp setup (pip install pyyaml).", file=sys.stderr)
        sys.exit(1)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False)
    print(f"  Written: {path}")



def update_managed_text(path, marker, block):
    """Replace or append one clearly delimited machine-managed section."""
    path = Path(path)
    start_marker = f"# >>> {marker} >>>"
    end_marker = f"# <<< {marker} <<<"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    meaningful = "\n".join(
        line for line in existing.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ).strip()
    managed = f"{start_marker}\n{block.rstrip()}\n{end_marker}\n"
    start = existing.find(start_marker)
    if start >= 0:
        end = existing.find(end_marker, start)
        if end < 0:
            print(f"Existing managed section is incomplete: {path}", file=sys.stderr)
            sys.exit(1)
        end += len(end_marker)
        updated = existing[:start] + managed + existing[end:].lstrip("\n")
    elif meaningful in ("", "[]"):
        updated = managed
    else:
        updated = existing.rstrip() + "\n\n" + managed

    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as stream:
        stream.write(updated)
        temporary_path = stream.name
    os.chmod(temporary_path, 0o600)
    os.replace(temporary_path, path)
    os.chmod(path, 0o600)
    print(f"  Written: {path}")


def ensure_omp_installed():
    """Install the Oh My Pi (omp) CLI via its official installer if missing."""
    if resolve_user_command("omp"):
        return
    print("  omp not found on PATH; installing via official installer...")
    subprocess.run(
        ["bash", "-c", "curl -fsSL https://omp.sh/install | sh"],
        check=True,
    )


def resolve_user_command(name):
    """Find a command on PATH or in an installed NVM Node version."""
    bun_command = Path(HOME) / ".bun" / "bin" / name
    if bun_command.is_file():
        return bun_command
    candidates = list((Path(HOME) / ".nvm" / "versions" / "node").glob(f"*/bin/{name}"))
    if candidates:
        return max(candidates, key=lambda path: path.stat().st_mtime)
    command = shutil.which(name)
    return Path(command) if command else None


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


def setup_omp(api_key, models):
    print("\n[Oh My Pi (omp)] Configuring v-rail provider with auto-discovery...")
    ensure_omp_installed()

    agent_dir = os.path.join(HOME, ".omp", "agent")

    # models.yml: v-rail chat provider (auto-discovered from /v1/models at
    # runtime, mirroring Claude Code gateway discovery) plus openai-codex for
    # OpenAI-style web search. The codex adapter calls {baseUrl}/codex/responses
    # and refuses OAuth for custom endpoints, so the pinned apiKey is required.
    models_path = os.path.join(agent_dir, "models.yml")
    models_data = {
        "providers": {
            "v-rail": {
                "baseUrl": f"{BASE_URL}/v1",
                "api": "openai-completions",
                "apiKey": api_key,
                "authHeader": True,
                "discovery": {"type": "openai-models-list"},
            },
            "openai-codex": {
                "baseUrl": f"{BASE_URL}/v1",
                "api": "openai-codex-responses",
                "apiKey": api_key,
            },
        }
    }
    write_yaml(models_path, models_data)

    # config.yml: configure model roles, conversation flow, look & feel, and tool policies.
    config_path = os.path.join(agent_dir, "config.yml")
    config_data = read_yaml(config_path)
    model_roles = config_data.setdefault("modelRoles", {})
    preferred_default = "v-rail/gemini-3.7-flash-high:medium" if any("gemini-3.7-flash-high" in m for m in models) else f"v-rail/{models[0]}"
    model_roles["default"] = preferred_default
    model_roles["advisor"] = "v-rail/gemini-3.7-flash-high"
    model_roles["smol"] = "v-rail/gemini-3.7-flash-high"
    model_roles["task"] = "v-rail/gpt-5.6-sol"

    config_data["steeringMode"] = "all"
    config_data["followUpMode"] = "all"
    config_data["interruptMode"] = "wait"

    config_data["symbolPreset"] = "unicode"
    config_data["composer"] = {"shape": "claude"}
    config_data["theme"] = {"dark": "titanium", "light": "light"}
    config_data["statusLine"] = {"preset": "nerd"}
    config_data["display"] = {"showTokenUsage": True}

    config_data["tools"] = {"approvalMode": "yolo"}
    disabled_providers = config_data.setdefault("disabledProviders", [])
    if "claude" not in disabled_providers:
        disabled_providers.append("claude")
    config_data.setdefault("providers", {})
    config_data["providers"]["webSearchOrder"] = ["codex"]
    write_yaml(config_path, config_data)
    print(f"  Default model: {config_data['modelRoles']['default']} (full list auto-discovered at runtime)")
    print("  Web search: OpenAI (codex) provider via v-rail, pinned first in webSearchOrder")
    print("  Flow: steeringMode=all, followUpMode=all, interruptMode=wait")
    print("  Appearance: theme=titanium/light, composer=claude, symbols=unicode, statusLine=nerd")

    # Keybindings: Tab / Ctrl+Enter / Ctrl+Q for follow-up; shortcuts for model picker
    keybindings_path = os.path.join(agent_dir, "keybindings.json")
    keybindings_data = read_json(keybindings_path)
    keybindings_data["app.message.followUp"] = ["Tab", "Ctrl+Enter", "Ctrl+Q"]
    keybindings_data["app.model.selectTemporary"] = ["Alt+M", "Alt+P"]
    keybindings_data["app.model.select"] = "Ctrl+Alt+M"
    write_json(keybindings_path, keybindings_data)

    keybindings_yml_path = os.path.join(agent_dir, "keybindings.yml")
    keybindings_yml_data = {
        "app.message.followUp": ["Tab", "Ctrl+Enter", "Ctrl+Q"],
        "app.model.select": "Ctrl+Alt+M",
        "app.model.selectTemporary": ["Alt+M", "Alt+P"],
    }
    write_yaml(keybindings_yml_path, keybindings_yml_data)
    print("  Keybindings: Tab/Ctrl+Enter/Ctrl+Q for follow-up; Alt+M/Alt+P for session model; Ctrl+Alt+M for roles")
    prompt_path = REPO_ROOT / "prompts" / "coding-rules.md"
    prompt_block = f"@{prompt_path}"
    update_managed_text(
        Path(agent_dir) / "AGENTS.md",
        "AIConfig prompt: coding-rules",
        prompt_block,
    )

    skills_dir = Path(HOME) / ".agents" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    linked_skills = 0
    for source in sorted((REPO_ROOT / "skill").iterdir()):
        if not source.is_dir() or not (source / "SKILL.md").is_file():
            continue
        target = skills_dir / source.name
        if target.is_symlink() and target.resolve() == source.resolve():
            linked_skills += 1
            continue
        if target.exists() or target.is_symlink():
            print(f"  Skipped existing non-AIConfig skill: {target}", file=sys.stderr)
            continue
        target.symlink_to(source, target_is_directory=True)
        linked_skills += 1
        print(f"  Linked skill: {source.name}")
    print(f"  Skills available to OMP: {linked_skills}")
    print(f"  Verify with: omp models find v-rail")


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
    "opencode": "OpenCode (~/.config/opencode/)",
    "kilocode": "KiloCode (~/.local/share/kilo/, ~/.cache/kilo/)",
    "codex": "Codex (~/.codex/)",
    "omp": "Oh My Pi (~/.omp/agent/)",
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
    parser.add_argument("--opencode", action="store_true", help="Setup OpenCode")
    parser.add_argument(
        "--kilo", "--kilocode", dest="kilocode", action="store_true",
        help="Setup KiloCode",
    )
    parser.add_argument("--codex", action="store_true", help="Setup Codex")
    parser.add_argument(
        "--omp", "--oh-my-pi", dest="omp", action="store_true",
        help="Setup Oh My Pi (omp)",
    )

    args = parser.parse_args()

    selected = []
    if args.claude:
        selected.append("claude")
    if args.opencode:
        selected.append("opencode")
    if args.kilocode:
        selected.append("kilocode")
    if args.codex:
        selected.append("codex")
    if args.omp:
        selected.append("omp")

    if not selected:
        parser.error(
            "Specify at least one agent: --claude, --opencode, "
            "--kilo/--kilocode, --codex, or --omp/--oh-my-pi"
        )

    print(f"Fetching models from {MODELS_ENDPOINT}...")
    api_key, models = resolve_api_key(args.api_key)
    print(f"Found {len(models)} models.")

    # Agents that do full overwrite of model lists
    overwrite_agents = [a for a in selected if a in ("opencode", "kilocode", "omp")]
    if overwrite_agents:
        confirm_overwrite([AGENT_NAMES[a] for a in overwrite_agents])

    if "claude" in selected:
        setup_claude(api_key, models)
    if "opencode" in selected:
        setup_opencode(api_key, models)
    if "kilocode" in selected:
        setup_kilocode(api_key, models)
    if "codex" in selected:
        setup_codex(api_key, models)
    if "omp" in selected:
        setup_omp(api_key, models)

    print("\nDone.")


if __name__ == "__main__":
    main()
