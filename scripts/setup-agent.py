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
CLIPROXYAPI_BASE_URL = os.environ.get("CLIPROXYAPI_BASE_URL", "http://127.0.0.1:4000")
CLIPROXYAPI_MODELS_ENDPOINT = f"{CLIPROXYAPI_BASE_URL}/v1/models"
CLIPROXYAPI_CONFIG = os.environ.get(
    "CLIPROXYAPI_CONFIG", "/home/nas/Projects/CLIProxyAPI/config.yaml"
)
DEFAULT_DSH_MODEL = "gpt-5.6-sol"
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


def find_cliproxyapi_api_key():
    """Return the first CLIProxyAPI key whose value contains exactly four hyphens."""
    config_path = Path(CLIPROXYAPI_CONFIG)
    if yaml is None:
        print("Error: PyYAML is required for CLIProxyAPI setup.", file=sys.stderr)
        sys.exit(1)
    if not config_path.exists():
        print(f"CLIProxyAPI config not found: {config_path}", file=sys.stderr)
        sys.exit(1)
    try:
        with config_path.open("r", encoding="utf-8") as stream:
            config = yaml.safe_load(stream) or {}
    except (OSError, yaml.YAMLError) as error:
        print(f"Could not read CLIProxyAPI config: {error}", file=sys.stderr)
        sys.exit(1)

    for value in config.get("api-keys", []):
        if isinstance(value, str) and value.count("-") == 4:
            return value

    print(
        "No CLIProxyAPI api-key with exactly four hyphens was found.",
        file=sys.stderr,
    )
    sys.exit(1)


def resolve_dsh_config():
    """Resolve the local proxy key and discover its complete model catalog."""
    api_key = find_cliproxyapi_api_key()
    try:
        models = fetch_models(api_key, CLIPROXYAPI_MODELS_ENDPOINT)
    except AuthError:
        print(
            "The selected CLIProxyAPI key failed to authenticate with the local proxy.",
            file=sys.stderr,
        )
        sys.exit(1)
    if DEFAULT_DSH_MODEL not in models:
        print(
            f"CLIProxyAPI did not advertise the required default model: {DEFAULT_DSH_MODEL}",
            file=sys.stderr,
        )
        sys.exit(1)
    return api_key, models


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


def read_dsh_yaml(path):
    """Read an existing DSH document without silently discarding bad data."""
    if not os.path.exists(path):
        return {}
    if yaml is None:
        print("Error: PyYAML is required for DSH setup.", file=sys.stderr)
        sys.exit(1)
    try:
        with open(path, "r", encoding="utf-8") as stream:
            data = yaml.safe_load(stream) or {}
    except (OSError, yaml.YAMLError) as error:
        print(f"Could not read DSH document {path}: {error}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict):
        print(f"DSH document must contain a mapping: {path}", file=sys.stderr)
        sys.exit(1)
    return data


def write_protected_yaml(path, data):
    """Write a DSH machine-local YAML document with owner-only permissions."""
    if yaml is None:
        print("Error: PyYAML is required for DSH setup.", file=sys.stderr)
        sys.exit(1)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    rendered = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as stream:
        stream.write(rendered)
        temporary_path = stream.name
    os.chmod(temporary_path, 0o600)
    os.replace(temporary_path, path)
    os.chmod(path, 0o600)
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

    # config.yml: pin a default role only if none is set yet (an existing
    # choice is preserved), and prefer the OpenAI (codex) search provider.
    config_path = os.path.join(agent_dir, "config.yml")
    config_data = read_yaml(config_path)
    config_data.setdefault("modelRoles", {})
    preferred_model = DEFAULT_DSH_MODEL if DEFAULT_DSH_MODEL in models else models[0]
    config_data["modelRoles"].setdefault("default", f"v-rail/{preferred_model}")
    config_data.setdefault("providers", {})
    config_data["providers"]["webSearchOrder"] = ["codex"]
    write_yaml(config_path, config_data)
    print(f"  Default model: {config_data['modelRoles']['default']} (full list auto-discovered at runtime)")
    print("  Web search: OpenAI (codex) provider via v-rail, pinned first in webSearchOrder")

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


def setup_dsh(api_key, models):
    """Install AIConfig's prompt, skills, proxy models, and web MCP into DSH."""
    dsh_command = resolve_user_command("dsh")
    if dsh_command is None:
        print("\n[DeepSeek Harness] Installing the official npm package...")
        npm_command = resolve_user_command("npm")
        if npm_command is None:
            print("npm is required to install DeepSeek Harness.", file=sys.stderr)
            sys.exit(1)
        subprocess.run(
            [str(npm_command), "install", "--global", "@deepseek-ai/dsh@latest"],
            check=True,
        )
        dsh_command = npm_command.with_name("dsh")
        if not dsh_command.is_file():
            dsh_command = resolve_user_command("dsh")
        if dsh_command is None:
            print("The DSH install completed but dsh was not found.", file=sys.stderr)
            sys.exit(1)

    dsh_home = Path(os.environ.get("DSH_HOME", os.path.join(HOME, ".dsh"))).expanduser()
    dsh_home.mkdir(parents=True, exist_ok=True)
    os.chmod(dsh_home, 0o700)

    print("\n[DeepSeek Harness] Configuring the local CLIProxyAPI provider...")
    settings_path = dsh_home / "settings.yaml"
    settings = read_dsh_yaml(settings_path)
    pi_ai = settings.setdefault("llm-pi-ai", {})
    providers = pi_ai.setdefault("providers", {})
    providers["cliproxyapi"] = {
        "displayName": "CLIProxyAPI",
        "apiKeyEnv": "CLIPROXYAPI_API_KEY",
        "api": "openai-completions",
        "baseURL": f"{CLIPROXYAPI_BASE_URL}/v1",
        "models": [{"id": model, "name": model} for model in models],
    }
    write_protected_yaml(settings_path, settings)
    print(f"  Provider: cliproxyapi ({len(models)} models discovered)")
    print(f"  Default model: {DEFAULT_DSH_MODEL} (DSH's built-in model selector remains enabled)")

    credentials_path = dsh_home / ".credentials.yaml"
    credentials = read_dsh_yaml(credentials_path)
    credentials["version"] = 1
    refs = credentials.setdefault("refs", {})
    if not isinstance(refs, dict):
        print(f"DSH credentials refs must be a mapping: {credentials_path}", file=sys.stderr)
        sys.exit(1)
    refs["CLIPROXYAPI_API_KEY"] = api_key
    credentials.setdefault("records", {})
    write_protected_yaml(credentials_path, credentials)

    prompt_path = REPO_ROOT / "prompts" / "coding-rules.md"
    if not prompt_path.is_file():
        print(f"AIConfig coding prompt not found: {prompt_path}", file=sys.stderr)
        sys.exit(1)
    prompt_block = (
        "# AIConfig prompt: coding-rules\n"
        f"<!-- installed from {prompt_path} by scripts/setup-agent.py -->\n\n"
        f"{prompt_path.read_text(encoding='utf-8').rstrip()}"
    )
    update_managed_text(dsh_home / "AGENTS.md", "AIConfig prompt: coding-rules", prompt_block)

    skills_dir = dsh_home / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(skills_dir, 0o700)
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
    print(f"  Skills available to DSH: {linked_skills}")

    # The Codex standalone route is preferred. DuckDuckGo is enabled as a
    # no-key fallback because a proxy account may not have web-search access.
    web_config = dsh_home / "web.toml"
    key_literal = json.dumps(api_key, ensure_ascii=False)
    web_config_text = f'''# Generated by AIConfig --dsh. Do not commit this file.

[search]
brave_cooldown_seconds = 1.0
duckduckgo_cooldown_seconds = 60.0
request_timeout_seconds = 30

[providers.brave]
command = "npx"
args = ["-y", "@brave/brave-search-mcp-server@2.1.0"]
api_keys = []

[providers.duckduckgo]
enabled = true
command = "uvx"
args = ["duckduckgo-mcp-server==0.6.1"]

[providers.codex_standalone]
api_keys = [{key_literal}]
base_url = "{CLIPROXYAPI_BASE_URL}/v1"
model = ""

[providers.tavily]
api_keys = []
endpoint = "https://mcp.tavily.com/mcp/"
'''
    web_config.write_text(web_config_text, encoding="utf-8")
    os.chmod(web_config, 0o600)
    print(f"  Written: {web_config}")

    patch_block = f'''- id: agent-default-model
  config:
    provider: cliproxyapi
    model: {DEFAULT_DSH_MODEL}
- insert:
    - id: mcp-aiconfig-web
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: web
        transport: stdio
        command: uv
        args:
          - run
          - --script
          - {REPO_ROOT / 'mcp' / 'web_search_mcp.py'}
          - --config
          - !!js dshHomePath('web.toml')
        failOnStartupError: true
'''
    update_managed_text(dsh_home / "cordis.patch.yml", "AIConfig DSH integration", patch_block)

    print("  Installing the maintained terminal UI...")
    dsh_env = os.environ.copy()
    dsh_env["PATH"] = os.pathsep.join(
        [str(dsh_command.parent), dsh_env.get("PATH", "")]
    )
    subprocess.run(
        [str(dsh_command), "plugin", "--profile", "martty", "add", "martty@latest"],
        check=True,
        env=dsh_env,
    )
    tui_patch = f'''- id: tui-acp-host
  config:
    provider: cliproxyapi
    model: {DEFAULT_DSH_MODEL}
'''
    update_managed_text(
        dsh_home / "profiles" / "martty" / "cordis.patch.yml",
        "AIConfig DSH TUI integration",
        tui_patch,
    )
    shell_function = '''dsh() {
  if [ "$#" -eq 0 ]; then
    command dsh --profile martty
  else
    command dsh "$@"
  fi
}'''
    update_managed_text(
        Path(HOME) / ".bashrc",
        "AIConfig DSH launcher",
        shell_function,
    )
    print("  Web MCP: mcp__web__search, mcp__web__fetch, mcp__web__research")
    print("  Codex search auth: selected CLIProxyAPI key (DuckDuckGo fallback enabled)")
    print("  Terminal UI: run dsh in a new Bash shell")


AGENT_NAMES = {
    "claude": "Claude Code (~/.claude/)",
    "droid": "Factory Droid (~/.factory/)",
    "opencode": "OpenCode (~/.config/opencode/)",
    "kilocode": "KiloCode (~/.local/share/kilo/, ~/.cache/kilo/)",
    "codex": "Codex (~/.codex/)",
    "omp": "Oh My Pi (~/.omp/agent/)",
    "dsh": "DeepSeek Harness (~/.dsh/)",
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
    parser.add_argument(
        "--omp", "--oh-my-pi", dest="omp", action="store_true",
        help="Setup Oh My Pi (omp)",
    )
    parser.add_argument(
        "--dsh", action="store_true",
        help="Setup DeepSeek Harness against the local CLIProxyAPI",
    )

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
    if args.omp:
        selected.append("omp")
    if args.dsh:
        selected.append("dsh")

    if not selected:
        parser.error(
            "Specify at least one agent: --claude, --droid/--factory, "
            "--opencode, --kilo/--kilocode, --codex, --omp/--oh-my-pi"
            ", or --dsh"
        )

    standard_selected = [agent for agent in selected if agent != "dsh"]
    api_key = models = None
    if standard_selected:
        print(f"Fetching models from {MODELS_ENDPOINT}...")
        api_key, models = resolve_api_key(args.api_key)
        print(f"Found {len(models)} models.")

    dsh_api_key = dsh_models = None
    if args.dsh:
        print(f"Fetching models from {CLIPROXYAPI_MODELS_ENDPOINT}...")
        dsh_api_key, dsh_models = resolve_dsh_config()
        print(f"Found {len(dsh_models)} models.")

    # Agents that do full overwrite of model lists
    overwrite_agents = [a for a in selected if a in ("droid", "opencode", "kilocode", "omp")]
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
    if "omp" in selected:
        setup_omp(api_key, models)
    if "dsh" in selected:
        setup_dsh(dsh_api_key, dsh_models)

    print("\nDone.")


if __name__ == "__main__":
    main()
