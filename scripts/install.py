#!/usr/bin/env python3
"""Unified interactive installer and configuration manager for AIConfig.

Merges and replaces install.sh, install.ps1, setup-agent.py, and sync-omp-config.py.
Supports Oh My Pi (omp), Codex, Claude Code, Freebuff, OpenCode, and KiloCode.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None  # type: ignore

try:
    import yaml
except ImportError:
    yaml = None

REPO_ROOT = Path(__file__).resolve().parents[1]
HOME = Path.home()
BASE_URL = "https://litellm.v-rail.org"
MODELS_ENDPOINT = f"{BASE_URL}/v1/models"
STANDALONE_SEARCH_ENDPOINT = f"{BASE_URL}/v1/alpha/search"

# Harness definitions: (name, id, default_home)
HARNESSES = [
    ("Oh My Pi (omp)", "omp", HOME / ".omp" / "agent"),
    ("Codex", "codex", HOME / ".codex"),
    ("Claude Code", "claude", HOME / ".claude"),
    ("Freebuff", "freebuff", HOME / ".agents"),
    ("OpenCode", "opencode", HOME / ".config" / "opencode"),
    ("KiloCode", "kilocode", HOME / ".local" / "share" / "kilo"),
]

HARNESS_MAP = {h[1]: h for h in HARNESSES}

# ANSI colors
if sys.stdout.isatty() and os.name != "nt":
    BOLD = "\033[1m"
    BLUE = "\033[1;34m"
    CYAN = "\033[1;36m"
    GREEN = "\033[1;32m"
    RED = "\033[1;31m"
    YELLOW = "\033[1;33m"
    DIM = "\033[2m"
    RESET = "\033[0m"
else:
    BOLD = BLUE = CYAN = GREEN = RED = YELLOW = DIM = RESET = ""


def success(msg: str) -> None:
    print(f"{GREEN}✓{RESET} {msg}")


def warning(msg: str) -> None:
    print(f"{YELLOW}!{RESET} {msg}", file=sys.stderr)


def failure(msg: str) -> None:
    print(f"{RED}x{RESET} {msg}", file=sys.stderr)


def read_json(path: Path | str) -> dict:
    p = Path(path)
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def write_json(path: Path | str, data: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=p.parent, delete=False) as f:
        json.dump(data, f, indent=2)
        f.write("\n")
        temp_path = f.name
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, p)
    success(f"Written: {p}")


def read_yaml(path: Path | str) -> dict:
    p = Path(path)
    if not p.is_file() or yaml is None:
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def write_yaml(path: Path | str, data: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if yaml is not None:
        content = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    else:
        # Simple fallback YAML emitter for basic dicts
        content = _dump_simple_yaml(data)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=p.parent, delete=False) as f:
        f.write(content)
        temp_path = f.name
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, p)
    success(f"Written: {p}")


def _dump_simple_yaml(data: dict, indent: int = 0) -> str:
    lines = []
    prefix = "  " * indent
    for k, v in data.items():
        if isinstance(v, dict):
            lines.append(f"{prefix}{k}:")
            lines.append(_dump_simple_yaml(v, indent + 1))
        elif isinstance(v, list):
            lines.append(f"{prefix}{k}:")
            for item in v:
                lines.append(f"{prefix}  - {json.dumps(item) if isinstance(item, (dict, list)) else item}")
        elif isinstance(v, bool):
            lines.append(f"{prefix}{k}: {'true' if v else 'false'}")
        elif v is None:
            lines.append(f"{prefix}{k}: null")
        else:
            lines.append(f"{prefix}{k}: {v}")
    return "\n".join(lines)


def read_toml(path: Path | str) -> dict:
    p = Path(path)
    if not p.is_file():
        return {}
    if tomllib is None:
        raise RuntimeError("tomllib is required (Python 3.11+ or tomli)")
    return tomllib.loads(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Header and Interactive Selection
# ---------------------------------------------------------------------------

def draw_header(target_name: str, target_home: Path, dry_run: bool) -> None:
    print(f"\n{BLUE}{'─' * 60}{RESET}")
    print(f"{BOLD}{CYAN} AIConfig Setup & Harness Manager{RESET}")
    print(f"{DIM}  Source: {REPO_ROOT}{RESET}")
    print(f"{DIM}  Target: {target_name} · {target_home}{RESET}")
    if dry_run:
        print(f"{YELLOW}  Mode:   preview only (no changes){RESET}")
    print(f"{BLUE}{'─' * 60}{RESET}")


def select_harness_interactive() -> tuple[str, str, Path]:
    """Ask the user which harness to install into without defaulting to Codex."""
    print(f"\n{BOLD}{CYAN}Select target harness to install into:{RESET}")
    for idx, (name, h_id, default_path) in enumerate(HARNESSES, 1):
        print(f"  {idx}) {name:<20} [{default_path}]")
    print(f"  q) Exit")

    while True:
        try:
            choice = input(f"Select harness [1-{len(HARNESSES)}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)

        if choice.lower() in ("q", "quit", "exit"):
            sys.exit(0)

        if choice.isdigit():
            val = int(choice)
            if 1 <= val <= len(HARNESSES):
                name, h_id, default_path = HARNESSES[val - 1]
                return name, h_id, default_path

        warning(f"Please choose a number between 1 and {len(HARNESSES)}.")


# ---------------------------------------------------------------------------
# 1. MCP Installation
# ---------------------------------------------------------------------------

def render_template(val: str, target_home: Path) -> str:
    return val.replace("{REPO_ROOT}", str(REPO_ROOT)).replace("{CODEX_HOME}", str(target_home))


def prepare_mcp_config(server_id: str, server: dict, target_home: Path, dry_run: bool) -> Path | None:
    template_rel = server.get("config_template")
    path_template = server.get("config_path")
    if not template_rel or not path_template:
        return None

    src_path = REPO_ROOT / template_rel
    dst_path = Path(render_template(path_template, target_home))

    if not src_path.is_file():
        warning(f"MCP configuration template not found: {src_path}")
        return None

    if dst_path.exists():
        print(f"Using existing MCP configuration: {dst_path}")
        return dst_path

    if dry_run:
        print(f"Dry run: would create MCP configuration: {dst_path}")
        return dst_path

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src_path, dst_path)
    os.chmod(dst_path, 0o600)
    success(f"Created editable MCP configuration: {dst_path}")
    return dst_path


def configure_mcp_providers(server_id: str, server: dict, config_path: Path | None, dry_run: bool) -> None:
    providers = server.get("providers", [])
    if not providers or not config_path or not config_path.exists():
        return

    if dry_run:
        print(f"Dry run: would open provider setup for {server_id}")
        return

    while True:
        print(f"\n{BOLD}{server_id} provider setup{RESET}")
        cfg = read_toml(config_path)

        for idx, prov in enumerate(providers, 1):
            label = prov.get("label", "")
            kind = prov.get("kind", "")
            table = prov.get("config_table", "")
            key = prov.get("config_key", "")

            # Look up in TOML
            curr = cfg
            for part in table.split("."):
                curr = curr.get(part, {}) if isinstance(curr, dict) else {}
            val = curr.get(key) if isinstance(curr, dict) else None

            if kind == "toggle":
                st = f"{GREEN}✓ enabled{RESET}" if val is True else f"{DIM}disabled{RESET}"
            else:
                st = f"{GREEN}✓ configured{RESET}" if isinstance(val, list) and bool(val) else f"{DIM}not configured{RESET}"

            print(f" {idx}) {label:<24} {st}")

        vim_opt = len(providers) + 1
        done_opt = len(providers) + 2
        print(f" {vim_opt}) Edit full config in editor")
        print(f" {done_opt}) Done and continue")

        try:
            choice = input(f"Select an option [1-{done_opt}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if choice == str(done_opt):
            break
        elif choice == str(vim_opt):
            editor = os.environ.get("EDITOR") or ("vim" if shutil.which("vim") else "nano")
            subprocess.run([editor, str(config_path)])
            continue

        if choice.isdigit() and 1 <= int(choice) <= len(providers):
            p = providers[int(choice) - 1]
            label = p.get("label", "")
            kind = p.get("kind", "")
            table = p.get("config_table", "")
            key = p.get("config_key", "")

            if kind == "toggle":
                _toggle_toml_bool(config_path, table, key)
            else:
                try:
                    secret = input(f"Enter {label} (API key): ").strip()
                except (EOFError, KeyboardInterrupt):
                    continue
                if secret:
                    _set_toml_list_key(config_path, table, key, secret)
                    success(f"Updated {label}")


def _toggle_toml_bool(path: Path, table: str, key: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    cfg = tomllib.loads("".join(lines))
    curr = cfg
    for part in table.split("."):
        curr = curr.get(part, {}) if isinstance(curr, dict) else {}
    val = curr.get(key) is True if isinstance(curr, dict) else False

    header = f"[{table}]"
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == header)
    except StopIteration:
        lines.append(f"\n{header}\n{key} = {'false' if val else 'true'}\n")
    else:
        end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
        rep = f"{key} = {'false' if val else 'true'}\n"
        for i in range(start + 1, end):
            if pattern.match(lines[i]):
                lines[i] = rep
                break
        else:
            lines.insert(start + 1, rep)

    path.write_text("".join(lines), encoding="utf-8")


def _set_toml_list_key(path: Path, table: str, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    header = f"[{table}]"
    rep = f"{key} = [{json.dumps(value)}]\n"
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == header)
    except StopIteration:
        lines.append(f"\n{header}\n{rep}")
    else:
        end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
        for i in range(start + 1, end):
            if pattern.match(lines[i]):
                lines[i] = rep
                break
        else:
            lines.insert(start + 1, rep)

    path.write_text("".join(lines), encoding="utf-8")


def install_mcps(h_id: str, target_home: Path, dry_run: bool) -> None:
    mcp_catalog_path = REPO_ROOT / "mcp.toml"
    if not mcp_catalog_path.is_file():
        failure(f"mcp.toml not found: {mcp_catalog_path}")
        return

    catalog = read_toml(mcp_catalog_path).get("mcp", {})
    if not catalog:
        warning("No MCPs defined in mcp.toml")
        return

    print(f"\n{BOLD}{BLUE}Available MCPs from mcp.toml:{RESET}")
    entries = list(catalog.items())
    for idx, (sid, sdata) in enumerate(entries, 1):
        label = sdata.get("label", sid)
        cat = sdata.get("category", "other")
        desc = sdata.get("description", "")
        print(f" {idx:2d}) {label:<22} [{cat}] - {desc}")
    print(f"  a) Install all")
    print(f"  b) Back")

    try:
        selection = input("Select MCP numbers (comma-separated), 'a' for all, or 'b': ").strip()
    except (EOFError, KeyboardInterrupt):
        return

    if selection.lower() in ("b", "back", ""):
        return

    selected_sids = []
    if selection.lower() == "a":
        selected_sids = [sid for sid, _ in entries]
    else:
        for part in selection.split(","):
            part = part.strip()
            if part.isdigit() and 1 <= int(part) <= len(entries):
                selected_sids.append(entries[int(part) - 1][0])

    if not selected_sids:
        warning("No valid MCPs selected.")
        return

    for sid in selected_sids:
        server = catalog[sid]
        transport = server.get("transport", "stdio")
        cfg_path = prepare_mcp_config(sid, server, target_home, dry_run)
        configure_mcp_providers(sid, server, cfg_path, dry_run)

        if dry_run:
            print(f"Dry run: would install MCP {sid} for {h_id}")
            continue

        if transport == "url":
            url = server.get("url", "")
            _write_mcp_to_harness(h_id, target_home, sid, {"url": url})
        else:
            cmd = render_template(server.get("command", ""), target_home)
            raw_args = server.get("args", [])
            args = [render_template(a, target_home) for a in raw_args]
            _write_mcp_to_harness(h_id, target_home, sid, {"command": cmd, "args": args})

        success(f"Installed MCP: {sid}")


def _write_mcp_to_harness(h_id: str, target_home: Path, server_id: str, mcp_def: dict) -> None:
    if h_id in ("omp", "freebuff"):
        mcp_file = target_home / "mcp.json"
        data = read_json(mcp_file)
        servers = data.setdefault("mcpServers", {})
        servers[server_id] = mcp_def
        write_json(mcp_file, data)
    elif h_id == "claude":
        settings_file = target_home / "settings.json"
        data = read_json(settings_file)
        servers = data.setdefault("mcpServers", {})
        servers[server_id] = mcp_def
        write_json(settings_file, data)
    elif h_id == "codex":
        # Call codex CLI or update config.toml
        if shutil.which("codex"):
            env = dict(os.environ, CODEX_HOME=str(target_home))
            subprocess.run(["codex", "mcp", "remove", server_id], env=env, capture_output=True)
            if "url" in mcp_def:
                subprocess.run(["codex", "mcp", "add", server_id, "--url", mcp_def["url"]], env=env)
            else:
                subprocess.run(["codex", "mcp", "add", server_id, "--", mcp_def["command"]] + mcp_def.get("args", []), env=env)
        else:
            config_path = target_home / "config.toml"
            lines = config_path.read_text().splitlines(keepends=True) if config_path.is_file() else []
            sec = f"[mcp_servers.{server_id}]\n"
            if "url" in mcp_def:
                sec += f'url = "{mcp_def["url"]}"\n'
            else:
                sec += f'command = "{mcp_def["command"]}"\nargs = {json.dumps(mcp_def.get("args", []))}\n'
            lines.append("\n" + sec)
            config_path.write_text("".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# 2. Prompt Installation
# ---------------------------------------------------------------------------

def install_prompt(h_id: str, target_home: Path, dry_run: bool) -> None:
    prompts_dir = REPO_ROOT / "prompts"
    prompts_catalog = REPO_ROOT / "prompts.toml"
    catalog = read_toml(prompts_catalog).get("prompts", {}) if prompts_catalog.is_file() else {}

    md_files = sorted(p for p in prompts_dir.glob("*.md") if p.name != "README.md")
    if not md_files:
        warning("No prompt markdown files found in prompts/")
        return

    print(f"\n{BOLD}{BLUE}Choose a prompt to install:{RESET}")
    for idx, p in enumerate(md_files, 1):
        name = p.stem
        info = catalog.get(name, {})
        label = info.get("label", name)
        desc = info.get("description", "")
        print(f" {idx:2d}) {label:<24} {desc}")
    print(f"  b) Back")

    try:
        choice = input(f"Select a prompt [1-{len(md_files)}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return

    if choice.lower() in ("b", "back", "") or not choice.isdigit():
        return

    val = int(choice)
    if not (1 <= val <= len(md_files)):
        return

    selected_prompt = md_files[val - 1]

    if dry_run:
        print(f"Dry run: would link/install {selected_prompt} into {h_id} ({target_home})")
        return

    target = (HOME / ".AGENTS.md") if h_id == "freebuff" else (target_home / ("CLAUDE.md" if h_id == "claude" else "AGENTS.md"))
    import_line = f"@{selected_prompt.resolve()}"
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    if import_line in existing:
        print(f"Already referenced in {target}")
        return

    block = f"\n# >>> AIConfig prompt: {selected_prompt.stem} >>>\n{import_line}\n# <<< AIConfig prompt: {selected_prompt.stem} <<<\n"
    with open(target, "a", encoding="utf-8") as f:
        f.write(block)
    success(f"Prompt referenced via '@' in {target}")

# ---------------------------------------------------------------------------
# 3. Skill Installation
# ---------------------------------------------------------------------------

def install_skills(h_id: str, target_home: Path, dry_run: bool) -> None:
    skills_catalog = REPO_ROOT / "skills.toml"
    skill_dir = REPO_ROOT / "skill"
    catalog = read_toml(skills_catalog).get("groups", {}) if skills_catalog.is_file() else {}

    available_skills = sorted(p.name for p in skill_dir.iterdir() if p.is_dir() and (p / "SKILL.md").is_file())
    if not available_skills:
        warning("No skills with SKILL.md found.")
        return

    print(f"\n{BOLD}{BLUE}Choose a skill set or individual skills:{RESET}")
    groups = list(catalog.items())
    for idx, (gid, ginfo) in enumerate(groups, 1):
        label = ginfo.get("label", gid)
        desc = ginfo.get("description", "")
        print(f" {idx:2d}) {label:<22} - {desc}")
    print(f"  i) Choose individual skills")
    print(f"  a) Install all discovered skills")
    print(f"  b) Back")

    try:
        choice = input("Select an option: ").strip()
    except (EOFError, KeyboardInterrupt):
        return

    selected: list[str] = []
    if choice.lower() in ("b", "back", ""):
        return
    elif choice.lower() == "a":
        selected = available_skills
    elif choice.lower() == "i":
        print(f"\n{BOLD}Individual skills:{RESET}")
        for idx, s in enumerate(available_skills, 1):
            print(f" {idx:2d}) {s}")
        sub = input("Select skill numbers (comma-separated) or 'a': ").strip()
        if sub.lower() == "a":
            selected = available_skills
        else:
            for part in sub.split(","):
                part = part.strip()
                if part.isdigit() and 1 <= int(part) <= len(available_skills):
                    selected.append(available_skills[int(part) - 1])
    elif choice.isdigit() and 1 <= int(choice) <= len(groups):
        gid, ginfo = groups[int(choice) - 1]
        members = ginfo.get("skills", [])
        if "*" in members:
            selected = available_skills
        else:
            selected = [s for s in members if s in available_skills]

    if not selected:
        warning("No skills selected.")
        return

    dest_skills_dir = (HOME / ".agents" / "skills") if h_id == "freebuff" else (target_home / "skills")
    if not dry_run:
        dest_skills_dir.mkdir(parents=True, exist_ok=True)

    for s in selected:
        src = skill_dir / s
        dst = dest_skills_dir / s
        if dst.is_symlink() and dst.resolve() == src.resolve():
            print(f"Already linked: {s}")
            continue
        if dry_run:
            print(f"Dry run: would link {s} -> {dst}")
            continue
        try:
            if dst.exists() or dst.is_symlink():
                if dst.is_symlink():
                    dst.unlink()
                else:
                    warning(f"Skipped existing non-symlink: {dst}")
                    continue
            dst.symlink_to(src, target_is_directory=True)
            success(f"Linked skill: {s}")
        except OSError:
            # Fallback for Windows without symlink permissions
            shutil.copytree(src, dst, dirs_exist_ok=True)
            success(f"Copied skill: {s}")


# ---------------------------------------------------------------------------
# 4. OMP / Tracked Configs (Apply / Sync)
# ---------------------------------------------------------------------------

SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:^|[^a-z0-9])(?:api[_-]?key|access[_-]?token|auth[_-]?token|"
    r"client[_-]?secret|password|credential|secret)(?:[^a-z0-9]|$)"
)
SETTINGS_FILES = ("config.yml", "keybindings.yml", "keybindings.json")


def manage_omp_configs(target_home: Path, dry_run: bool) -> None:
    omp_dir = REPO_ROOT / "config" / "omp"
    print(f"\n{BOLD}{BLUE}OMP Tracked Settings (config/omp):{RESET}")
    print(" 1) Apply tracked settings to ~/.omp/agent/ (Restore config.yml, keybindings)")
    print(" 2) Copy existing ~/.omp/agent/ settings into config/omp/ (Sync repository)")
    print(" b) Back")

    try:
        opt = input("Select an option [1-2]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return

    if opt == "1":
        _apply_omp_settings(omp_dir, target_home, dry_run)
    elif opt == "2":
        _sync_omp_settings(target_home, omp_dir, dry_run)


def _apply_omp_settings(src_dir: Path, dst_dir: Path, dry_run: bool) -> None:
    if not src_dir.is_dir():
        failure(f"Tracked settings directory not found: {src_dir}")
        return

    for fname in SETTINGS_FILES:
        src = src_dir / fname
        if not src.is_file():
            continue
        dst = dst_dir / fname
        if dry_run:
            print(f"Dry run: would install {fname} into {dst}")
            continue
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        os.chmod(dst, 0o600)
        success(f"Installed OMP setting: {fname}")

    if shutil.which("omp") and not dry_run:
        print("Refreshing OMP models catalog...")
        subprocess.run(["omp", "models", "refresh"], capture_output=True)


def _sync_omp_settings(src_dir: Path, dst_dir: Path, dry_run: bool) -> None:
    if not src_dir.is_dir():
        failure(f"OMP directory not found: {src_dir}")
        return

    settings: dict[str, str] = {}
    for name in SETTINGS_FILES:
        p = src_dir / name
        if not p.is_file():
            continue
        content = p.read_text(encoding="utf-8")
        # Check for secrets
        if p.suffix == ".json":
            try:
                data = json.loads(content)
                _check_json_sensitive(data, p)
            except ValueError as e:
                failure(str(e))
                return
        else:
            for line in content.splitlines():
                m = re.match(r"^\s*([A-Za-z0-9_.-]+)\s*[:=]", line)
                if m and SENSITIVE_KEY_RE.search(m.group(1)):
                    failure(f"Refusing {p}: sensitive key found: {m.group(1)}")
                    return
        settings[name] = content

    if not settings:
        warning("No allowlisted settings found to sync.")
        return

    if dry_run:
        for name in settings:
            print(f"Dry run: would copy {name} to {dst_dir}")
        return

    dst_dir.mkdir(parents=True, exist_ok=True)
    for name, content in settings.items():
        (dst_dir / name).write_text(content, encoding="utf-8")
        success(f"Synced to repository: {name}")


def _check_json_sensitive(obj: object, path: Path) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if SENSITIVE_KEY_RE.search(str(k)):
                raise ValueError(f"Refusing {path}: sensitive key found: {k}")
            _check_json_sensitive(v, path)
    elif isinstance(obj, list):
        for it in obj:
            _check_json_sensitive(it, path)


# ---------------------------------------------------------------------------
# 5. Configure Models, Provider & LiteLLM (setup-agent merge)
# ---------------------------------------------------------------------------

def fetch_models(api_key: str) -> list[str]:
    req = urllib.request.Request(MODELS_ENDPOINT)
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("User-Agent", "aiconfig-installer/1.0")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        return sorted(m["id"] for m in data.get("data", []))
    except Exception as e:
        warning(f"Could not fetch models from {MODELS_ENDPOINT}: {e}")
        return []


def detect_standalone_web_search(api_key: str, models: list[str]) -> bool:
    preferred = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.4")
    model = next((m for m in preferred if m in models), next((m for m in models if m.lower().startswith("gpt-")), None))
    if not model:
        return False
    payload = json.dumps({
        "id": f"setup-{uuid.uuid4()}",
        "model": model,
        "commands": {"search_query": [{"q": "OpenAI"}]},
    }).encode()
    req = urllib.request.Request(STANDALONE_SEARCH_ENDPOINT, data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception:
        return False


def find_existing_api_key() -> tuple[str | None, str | None]:
    candidates = [
        (HOME / ".claude" / "settings.json", lambda d: d.get("env", {}).get("ANTHROPIC_AUTH_TOKEN") if d.get("env", {}).get("ANTHROPIC_BASE_URL") == BASE_URL else None),
        (HOME / ".omp" / "agent" / "models.yml", lambda d: next((p.get("apiKey") for p in d.get("providers", {}).values() if str(p.get("baseUrl", "")).startswith(BASE_URL) and p.get("apiKey")), None) if yaml else None),
        (HOME / ".config" / "opencode" / "opencode.json", lambda d: next((p.get("options", {}).get("apiKey") for p in d.get("provider", {}).values() if str(p.get("options", {}).get("baseURL", "")).startswith(BASE_URL) and p.get("options", {}).get("apiKey")), None)),
        (HOME / ".local" / "share" / "kilo" / "auth.json", lambda d: d.get("cliproxyapi", {}).get("key")),
        (HOME / ".codex" / "auth.json", lambda d: d.get("OPENAI_API_KEY")),
    ]
    for path, extractor in candidates:
        if path.is_file():
            try:
                if path.suffix in (".yaml", ".yml"):
                    data = read_yaml(path)
                else:
                    data = read_json(path)
                key = extractor(data)
                if key:
                    return key, str(path)
            except Exception:
                pass
    return None, None


def setup_models_and_provider(h_id: str, target_home: Path, dry_run: bool) -> None:
    print(f"\n{BOLD}{CYAN}Configure LiteLLM / v-rail Provider & Models for {h_id}{RESET}")

    existing_key, src = find_existing_api_key()
    api_key = existing_key
    if existing_key:
        print(f"Found existing API key from {src}")
        try:
            use_existing = input("Use this existing API key? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return
        if use_existing in ("n", "no"):
            api_key = None

    if not api_key:
        try:
            api_key = input("Enter API key for v-rail (litellm.v-rail.org): ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not api_key:
            warning("No API key entered; aborted.")
            return

    print("Fetching models from LiteLLM endpoint...")
    models = fetch_models(api_key)
    if not models:
        warning("No models returned or authentication failed.")
        return
    print(f"Retrieved {len(models)} models.")

    if dry_run:
        print(f"Dry run: would configure provider and model settings for {h_id}")
        return

    if h_id == "omp":
        _setup_omp_provider(target_home, api_key, models)
    elif h_id == "codex":
        _setup_codex_provider(target_home, api_key, models)
    elif h_id == "claude":
        _setup_claude_provider(target_home, api_key, models)
    elif h_id == "opencode":
        _setup_opencode_provider(target_home, api_key, models)
    elif h_id == "kilocode":
        _setup_kilocode_provider(target_home, api_key, models)
    else:
        # Generic setup for other harnesses
        success(f"Models and API key verified: {len(models)} models.")


def _setup_omp_provider(target_home: Path, api_key: str, models: list[str]) -> None:
    models_path = target_home / "models.yml"
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

    config_path = target_home / "config.yml"
    config_data = read_yaml(config_path) if config_path.is_file() else {}
    roles = config_data.setdefault("modelRoles", {})
    preferred = "v-rail/gemini-3.7-flash-high:medium" if any("gemini-3.7-flash-high" in m for m in models) else f"v-rail/{models[0]}"
    roles["default"] = preferred
    roles["advisor"] = "v-rail/gemini-3.7-flash-high"
    roles["smol"] = "v-rail/gemini-3.7-flash-high"
    roles["task"] = "v-rail/gpt-5.6-sol"

    config_data["steeringMode"] = "all"
    config_data["followUpMode"] = "all"
    config_data["interruptMode"] = "wait"
    config_data["symbolPreset"] = "unicode"
    config_data["composer"] = {"shape": "claude"}
    config_data["theme"] = {"dark": "titanium", "light": "light"}
    config_data["statusLine"] = {"preset": "nerd"}
    config_data["display"] = {"showTokenUsage": True}
    config_data["tools"] = {"approvalMode": "yolo"}
    config_data.setdefault("providers", {})["webSearchOrder"] = ["codex"]
    write_yaml(config_path, config_data)

    keybindings_path = target_home / "keybindings.json"
    kb_data = read_json(keybindings_path)
    kb_data["app.message.followUp"] = ["Tab", "Ctrl+Enter", "Ctrl+Q"]
    kb_data["app.model.selectTemporary"] = ["Alt+M", "Alt+P"]
    kb_data["app.model.select"] = "Ctrl+Alt+M"
    write_json(keybindings_path, kb_data)

    success("Configured OMP v-rail provider, model roles, and keybindings.")


def _setup_codex_provider(target_home: Path, api_key: str, models: list[str]) -> None:
    auth_path = target_home / "auth.json"
    auth_data = read_json(auth_path)
    auth_data["auth_mode"] = "apikey"
    auth_data["OPENAI_API_KEY"] = api_key
    write_json(auth_path, auth_data)

    enable_search = detect_standalone_web_search(api_key, models)
    config_path = target_home / "config.toml"
    lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True) if config_path.is_file() else []

    filtered = []
    skip = False
    for line in lines:
        st = line.strip()
        if st.startswith("[") and st.endswith("]"):
            skip = (st == "[model_providers.v-rail]")
        if not skip:
            filtered.append(line)

    sec = f"\n[model_providers.v-rail]\nname = \"v-rail\"\nbase_url = \"{BASE_URL}/v1\"\nwire_api = \"responses\"\nrequires_openai_auth = true\n"
    if enable_search:
        sec += "supports_standalone_web_search = true\n"

    target_home.mkdir(parents=True, exist_ok=True)
    config_path.write_text("".join(filtered).rstrip() + "\n\n" + sec, encoding="utf-8")
    success("Configured Codex v-rail provider and auth.")


def _setup_claude_provider(target_home: Path, api_key: str, models: list[str]) -> None:
    settings_path = target_home / "settings.json"
    data = read_json(settings_path)
    env = data.setdefault("env", {})
    env["ANTHROPIC_BASE_URL"] = BASE_URL
    env["ANTHROPIC_AUTH_TOKEN"] = api_key
    env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] = "1"
    data.setdefault("permissions", {})["defaultMode"] = "bypassPermissions"
    write_json(settings_path, data)
    success("Configured Claude Code base URL, auth token, and gateway discovery.")


def _setup_opencode_provider(target_home: Path, api_key: str, models: list[str]) -> None:
    path = target_home / "opencode.json"
    data = read_json(path)
    data.setdefault("provider", {})["cliproxyapi"] = {
        "npm": "@ai-sdk/openai-compatible",
        "name": "CLIProxyAPI v-rail",
        "options": {"baseURL": f"{BASE_URL}/v1", "apiKey": api_key},
        "models": {mid: {"name": mid} for mid in models},
    }
    data["model"] = f"cliproxyapi/{models[0]}"
    data["small_model"] = f"cliproxyapi/{models[0]}"
    write_json(path, data)
    success("Configured OpenCode provider and models.")


def _setup_kilocode_provider(target_home: Path, api_key: str, models: list[str]) -> None:
    auth_path = target_home / "auth.json"
    auth_data = read_json(auth_path)
    auth_data["cliproxyapi"] = {"type": "api", "key": api_key}
    write_json(auth_path, auth_data)
    success("Configured KiloCode auth.")


# ---------------------------------------------------------------------------
# Main Menu & CLI Execution
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Unified AIConfig installer: MCPs, Prompts, Skills, OMP settings, and LiteLLM models."
    )
    parser.add_argument("--dry-run", action="store_true", help="Report changes without writing files")
    parser.add_argument("--harness", choices=[h[1] for h in HARNESSES], help="Target harness (e.g. omp, codex, claude)")
    parser.add_argument("--target-home", type=Path, help="Override target home path")
    parser.add_argument("--omp", action="store_true", help="Shortcut to select Oh My Pi (~/.omp/agent)")
    parser.add_argument("--codex", action="store_true", help="Shortcut to select Codex (~/.codex)")
    parser.add_argument("--claude", action="store_true", help="Shortcut to select Claude Code (~/.claude)")
    parser.add_argument("--freebuff", action="store_true", help="Shortcut to select Freebuff (~/.agents)")
    parser.add_argument("--models-only", action="store_true", help="Directly launch model & provider setup without menu")

    args = parser.parse_args()

    # Determine harness
    selected_harness_id = None
    if args.omp:
        selected_harness_id = "omp"
    elif args.codex:
        selected_harness_id = "codex"
    elif args.claude:
        selected_harness_id = "claude"
    elif args.freebuff:
        selected_harness_id = "freebuff"
    elif args.harness:
        selected_harness_id = args.harness

    if selected_harness_id:
        target_name, h_id, default_home = HARNESS_MAP[selected_harness_id]
        target_home = args.target_home or default_home
    else:
        # Prompt user interactively instead of defaulting to Codex!
        target_name, h_id, default_home = select_harness_interactive()
        target_home = args.target_home or default_home

    if args.models_only:
        setup_models_and_provider(h_id, target_home, args.dry_run)
        return

    while True:
        draw_header(target_name, target_home, args.dry_run)
        print(f"What would you like to set up?")
        print(f"  1) Install MCPs             Select from catalog in mcp.toml")
        print(f"  2) Install a prompt         Choose global instruction set (prompts.toml)")
        print(f"  3) Install skills           Choose skill set or individual skills (skills.toml)")
        print(f"  4) OMP / Tracked Configs    Apply or sync tracked config/omp settings")
        print(f"  5) Configure Models & Auth  Setup litellm.v-rail.org provider & model configs")
        print(f"  6) Switch target harness    Current: {target_name}")
        print(f"  7) Exit")

        try:
            choice = input(f"Select an option [1-7]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if choice == "1":
            install_mcps(h_id, target_home, args.dry_run)
        elif choice == "2":
            install_prompt(h_id, target_home, args.dry_run)
        elif choice == "3":
            install_skills(h_id, target_home, args.dry_run)
        elif choice == "4":
            manage_omp_configs(target_home, args.dry_run)
        elif choice == "5":
            setup_models_and_provider(h_id, target_home, args.dry_run)
        elif choice == "6":
            target_name, h_id, default_home = select_harness_interactive()
            target_home = args.target_home or default_home
        elif choice in ("7", "q", "quit", "exit"):
            break
        else:
            warning("Please choose an option from 1 to 7.")


if __name__ == "__main__":
    main()
