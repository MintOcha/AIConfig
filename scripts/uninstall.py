#!/usr/bin/env python3
"""Unified uninstaller for AIConfig.

Removes AIConfig MCPs, prompts (@ references), skills, and model configurations
from Oh My Pi (omp), Codex, Claude Code, Freebuff, OpenCode, and KiloCode.
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

HARNESSES = [
    ("Oh My Pi (omp)", "omp", HOME / ".omp" / "agent"),
    ("Codex", "codex", HOME / ".codex"),
    ("Claude Code", "claude", HOME / ".claude"),
    ("Freebuff", "freebuff", HOME / ".agents"),
    ("OpenCode", "opencode", HOME / ".config" / "opencode"),
    ("KiloCode", "kilocode", HOME / ".local" / "share" / "kilo"),
]

HARNESS_MAP = {h[1]: h for h in HARNESSES}

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
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    success(f"Updated: {p}")


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
        content = json.dumps(data, indent=2)
    p.write_text(content + "\n", encoding="utf-8")
    success(f"Updated: {p}")


def draw_header(target_name: str, target_home: Path, dry_run: bool) -> None:
    print(f"\n{RED}{'─' * 60}{RESET}")
    print(f"{BOLD}{RED} AIConfig Uninstaller{RESET}")
    print(f"{DIM}  Target: {target_name} · {target_home}{RESET}")
    if dry_run:
        print(f"{YELLOW}  Mode:   preview only (no changes){RESET}")
    print(f"{RED}{'─' * 60}{RESET}")


def select_harness_interactive() -> tuple[str, str, Path]:
    print(f"\n{BOLD}{CYAN}Select harness to uninstall from:{RESET}")
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

def get_mcp_folder_scripts() -> dict[str, Path]:
    """Discover all python MCP scripts in the mcp folder and their canonical stems."""
    mcp_dir = (REPO_ROOT / "mcp").resolve()
    if not mcp_dir.is_dir():
        return {}
    return {p.name: p for p in mcp_dir.glob("*.py")}


def _is_aiconfig_mcp_entry(server_data: dict | str, mcp_scripts: dict[str, Path]) -> bool:
    """Determine if a configured MCP server points to a script inside AIConfig/mcp."""
    repo_mcp_dir = str((REPO_ROOT / "mcp").resolve()).replace("\\", "/")
    raw_text = json.dumps(server_data).replace("\\", "/")

    # Direct path match to AIConfig/mcp
    if repo_mcp_dir in raw_text:
        return True

    # Match against known script filenames or their resolved symlinks
    for fname, path in mcp_scripts.items():
        resolved = str(path.resolve()).replace("\\", "/")
        if fname in raw_text or resolved in raw_text:
            return True

    return False


# ---------------------------------------------------------------------------
# 1. Uninstall MCPs
# ---------------------------------------------------------------------------

def uninstall_mcps(h_id: str, target_home: Path, dry_run: bool) -> None:
    mcp_scripts = get_mcp_folder_scripts()
    print(f"\n{BOLD}Scanning for installed MCPs originating from {REPO_ROOT / 'mcp'} in {h_id}...{RESET}")

    if h_id in ("omp", "freebuff"):
        mcp_file = target_home / "mcp.json"
        if mcp_file.is_file():
            data = read_json(mcp_file)
            servers = data.get("mcpServers", {})
            removed = [sid for sid, sdef in servers.items() if _is_aiconfig_mcp_entry(sdef, mcp_scripts)]

            if dry_run:
                print(f"Dry run: would remove {removed} from {mcp_file}")
                return

            if removed:
                for sid in removed:
                    del servers[sid]
                write_json(mcp_file, data)
                success(f"Removed MCPs from {mcp_file}: {', '.join(removed)}")
            else:
                print("No AIConfig MCPs found in mcp.json")

    elif h_id == "claude":
        settings_file = target_home / "settings.json"
        if settings_file.is_file():
            data = read_json(settings_file)
            servers = data.get("mcpServers", {})
            removed = [sid for sid, sdef in servers.items() if _is_aiconfig_mcp_entry(sdef, mcp_scripts)]

            if dry_run:
                print(f"Dry run: would remove {removed} from {settings_file}")
                return

            if removed:
                for sid in removed:
                    del servers[sid]
                write_json(settings_file, data)
                success(f"Removed MCPs from {settings_file}: {', '.join(removed)}")
            else:
                print("No AIConfig MCPs found in settings.json")

    elif h_id == "codex":
        config_file = target_home / "config.toml"
        if config_file.is_file():
            repo_mcp_dir = str((REPO_ROOT / "mcp").resolve()).replace("\\", "/")
            lines = config_file.read_text(encoding="utf-8").splitlines(keepends=True)
            # Parse TOML sections to find which mcp_servers.* sections contain AIConfig mcp paths
            sections_to_remove: set[str] = set()
            current_section = None
            section_lines: dict[str, list[str]] = {}

            for line in lines:
                st = line.strip()
                if st.startswith("[") and st.endswith("]"):
                    current_section = st.strip("[]")
                    section_lines[current_section] = [line]
                elif current_section:
                    section_lines[current_section].append(line)

            for sec, slines in section_lines.items():
                if sec.startswith("mcp_servers."):
                    block_text = "".join(slines).replace("\\", "/")
                    if repo_mcp_dir in block_text or any(fname in block_text for fname in mcp_scripts):
                        # extract top server name: mcp_servers.<name>
                        parts = sec.split(".")
                        server_name = parts[1]
                        sections_to_remove.add(server_name)

            if dry_run:
                print(f"Dry run: would remove Codex MCPs {sorted(sections_to_remove)} from {config_file}")
                return

            if sections_to_remove:
                if shutil.which("codex"):
                    env = dict(os.environ, CODEX_HOME=str(target_home))
                    for sname in sections_to_remove:
                        subprocess.run(["codex", "mcp", "remove", sname], env=env, capture_output=True)
                        success(f"Removed Codex MCP: {sname}")
                else:
                    prefixes = [f"mcp_servers.{s}" for s in sections_to_remove]
                    _remove_toml_sections(config_file, prefixes)
                    success(f"Removed Codex MCPs: {', '.join(sections_to_remove)}")
            else:
                print("No AIConfig MCPs found in Codex configuration")

# ---------------------------------------------------------------------------
# 2. Uninstall Prompt (@ references)
# ---------------------------------------------------------------------------

def uninstall_prompts(h_id: str, target_home: Path, dry_run: bool) -> None:
    target = (HOME / ".AGENTS.md") if h_id == "freebuff" else (target_home / ("CLAUDE.md" if h_id == "claude" else "AGENTS.md"))
    if not target.is_file():
        print(f"No prompt file found at {target}")
        return

    print(f"\n{BOLD}Scanning and removing '@' prompt references from {target}...{RESET}")
    content = target.read_text(encoding="utf-8")
    repo_str = str(REPO_ROOT).replace("\\", "/")
    prompts_dir = (REPO_ROOT / "prompts").resolve()
    known_prompt_names = {p.name for p in prompts_dir.glob("*.md")}

    lines = content.splitlines()
    filtered: list[str] = []
    skip_managed = False
    removed_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if "# >>> AIConfig prompt:" in stripped:
            skip_managed = True
            removed_lines.append(stripped)
            continue
        if "# <<< AIConfig prompt:" in stripped:
            skip_managed = False
            removed_lines.append(stripped)
            continue
        if skip_managed:
            removed_lines.append(stripped)
            continue

        # Detect '@' reference syntax targeting AIConfig prompts
        if stripped.startswith("@"):
            ref_path = stripped.removeprefix("@").strip().strip("\"'").replace("\\", "/")
            ref_target = Path(ref_path)
            is_aiconfig_ref = (
                repo_str in ref_path
                or "AIConfig" in ref_path
                or ref_target.name in known_prompt_names
                or (ref_target.is_file() and ref_target.resolve().parent == prompts_dir)
            )
            if is_aiconfig_ref:
                removed_lines.append(stripped)
                continue

        filtered.append(line)

    if not removed_lines and len(filtered) == len(lines):
        print(f"No AIConfig prompt references found in {target}")
        return

    for rem in removed_lines:
        print(f"Found and removing: {rem}")

    if dry_run:
        print(f"Dry run: would remove {len(removed_lines)} prompt reference(s) from {target}")
        return

    cleaned = "\n".join(filtered).strip()
    if cleaned:
        target.write_text(cleaned + "\n", encoding="utf-8")
        success(f"Cleaned prompt references in {target}")
    else:
        target.unlink()
        success(f"Removed empty file: {target}")

# ---------------------------------------------------------------------------
# 3. Uninstall Skills
# ---------------------------------------------------------------------------

def uninstall_skills(h_id: str, target_home: Path, dry_run: bool) -> None:
    dest_dir = (HOME / ".agents" / "skills") if h_id == "freebuff" else (target_home / "skills")
    if not dest_dir.is_dir():
        print(f"No skills directory found at {dest_dir}")
        return

    skill_names = [p.name for p in (REPO_ROOT / "skill").iterdir() if p.is_dir()]
    print(f"\n{BOLD}Removing AIConfig skills from {dest_dir}...{RESET}")

    removed = []
    for s in skill_names:
        target = dest_dir / s
        if target.is_symlink() or target.exists():
            if dry_run:
                print(f"Dry run: would remove skill {s} from {dest_dir}")
                continue
            if target.is_symlink() or target.is_file():
                target.unlink()
            else:
                shutil.rmtree(target)
            removed.append(s)

    if removed:
        success(f"Removed skills: {', '.join(removed)}")
    else:
        print("No AIConfig skills found to remove.")


# ---------------------------------------------------------------------------
# 4. Uninstall Model & Provider Configs
# ---------------------------------------------------------------------------

def uninstall_provider_configs(h_id: str, target_home: Path, dry_run: bool) -> None:
    print(f"\n{BOLD}Removing v-rail / LiteLLM provider configs from {h_id}...{RESET}")

    if dry_run:
        print(f"Dry run: would remove v-rail provider configs for {h_id}")
        return

    if h_id == "omp":
        models_file = target_home / "models.yml"
        if models_file.is_file():
            data = read_yaml(models_file)
            providers = data.get("providers", {})
            for k in ("v-rail", "openai-codex"):
                providers.pop(k, None)
            write_yaml(models_file, data)

        config_file = target_home / "config.yml"
        if config_file.is_file():
            cfg = read_yaml(config_file)
            roles = cfg.get("modelRoles", {})
            for r in ("default", "advisor", "smol", "task"):
                if "v-rail" in str(roles.get(r, "")):
                    roles.pop(r, None)
            write_yaml(config_file, cfg)

        success("Cleaned OMP models and provider configuration.")

    elif h_id == "codex":
        config_file = target_home / "config.toml"
        if config_file.is_file():
            _remove_toml_sections(config_file, ["model_providers.v-rail"])
            # Remove model_provider = "v-rail" line
            lines = [l for l in config_file.read_text().splitlines(keepends=True) if not re.match(r'^\s*model_provider\s*=\s*"v-rail"', l)]
            config_file.write_text("".join(lines), encoding="utf-8")
            success("Cleaned Codex v-rail provider configuration.")

    elif h_id == "claude":
        settings_file = target_home / "settings.json"
        if settings_file.is_file():
            data = read_json(settings_file)
            env = data.get("env", {})
            if "v-rail" in env.get("ANTHROPIC_BASE_URL", ""):
                env.pop("ANTHROPIC_BASE_URL", None)
                env.pop("ANTHROPIC_AUTH_TOKEN", None)
                env.pop("CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY", None)
                write_json(settings_file, data)
                success("Cleaned Claude Code v-rail settings.")

    elif h_id == "opencode":
        opencode_file = target_home / "opencode.json"
        if opencode_file.is_file():
            data = read_json(opencode_file)
            if "cliproxyapi" in data.get("provider", {}):
                del data["provider"]["cliproxyapi"]
                write_json(opencode_file, data)
                success("Cleaned OpenCode cliproxyapi provider.")

    elif h_id == "kilocode":
        auth_file = target_home / "auth.json"
        if auth_file.is_file():
            data = read_json(auth_file)
            if "cliproxyapi" in data:
                del data["cliproxyapi"]
                write_json(auth_file, data)
                success("Cleaned KiloCode cliproxyapi auth.")


# ---------------------------------------------------------------------------
# Main Menu & CLI Execution
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Uninstall AIConfig components (MCPs, prompts, skills, provider configs) from an agent harness."
    )
    parser.add_argument("--dry-run", action="store_true", help="Report changes without modifying files")
    parser.add_argument("--harness", choices=[h[1] for h in HARNESSES], help="Target harness (e.g. omp, codex, claude)")
    parser.add_argument("--target-home", type=Path, help="Override target home path")
    parser.add_argument("--omp", action="store_true", help="Shortcut to select Oh My Pi (~/.omp/agent)")
    parser.add_argument("--codex", action="store_true", help="Shortcut to select Codex (~/.codex)")
    parser.add_argument("--claude", action="store_true", help="Shortcut to select Claude Code (~/.claude)")
    parser.add_argument("--freebuff", action="store_true", help="Shortcut to select Freebuff (~/.agents)")
    parser.add_argument("--all", action="store_true", help="Uninstall everything for the selected harness non-interactively")

    args = parser.parse_args()

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
        target_name, h_id, default_home = select_harness_interactive()
        target_home = args.target_home or default_home

    if args.all:
        uninstall_mcps(h_id, target_home, args.dry_run)
        uninstall_prompts(h_id, target_home, args.dry_run)
        uninstall_skills(h_id, target_home, args.dry_run)
        uninstall_provider_configs(h_id, target_home, args.dry_run)
        success(f"Full uninstallation complete for {target_name}.")
        return

    while True:
        draw_header(target_name, target_home, args.dry_run)
        print("What would you like to uninstall?")
        print("  1) Uninstall MCPs                 Remove AIConfig MCP entries")
        print("  2) Uninstall Prompts              Remove @ prompt references from AGENTS.md/CLAUDE.md")
        print("  3) Uninstall Skills               Remove AIConfig skill links")
        print("  4) Uninstall Model & Provider     Remove v-rail / LiteLLM provider configs")
        print("  5) Uninstall Everything           Full removal of all AIConfig components")
        print(f"  6) Switch target harness          Current: {target_name}")
        print("  7) Exit")

        try:
            choice = input("Select an option [1-7]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if choice == "1":
            uninstall_mcps(h_id, target_home, args.dry_run)
        elif choice == "2":
            uninstall_prompts(h_id, target_home, args.dry_run)
        elif choice == "3":
            uninstall_skills(h_id, target_home, args.dry_run)
        elif choice == "4":
            uninstall_provider_configs(h_id, target_home, args.dry_run)
        elif choice == "5":
            uninstall_mcps(h_id, target_home, args.dry_run)
            uninstall_prompts(h_id, target_home, args.dry_run)
            uninstall_skills(h_id, target_home, args.dry_run)
            uninstall_provider_configs(h_id, target_home, args.dry_run)
            success(f"Full uninstallation complete for {target_name}.")
        elif choice == "6":
            target_name, h_id, default_home = select_harness_interactive()
            target_home = args.target_home or default_home
        elif choice in ("7", "q", "quit", "exit"):
            break
        else:
            warning("Please choose an option from 1 to 7.")


if __name__ == "__main__":
    main()
