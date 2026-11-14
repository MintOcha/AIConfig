"""Tests for Antigravity (agy) harness support in install.py and uninstall.py."""

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


install = _load_module("aiconfig_install", REPO_ROOT / "scripts" / "install.py")
uninstall = _load_module("aiconfig_uninstall", REPO_ROOT / "scripts" / "uninstall.py")


def test_agy_harness_registered():
    assert "agy" in install.HARNESS_MAP
    assert "agy" in uninstall.HARNESS_MAP
    assert install.HARNESS_MAP["agy"][2] == install.HOME / ".gemini" / "config"
    assert uninstall.HARNESS_MAP["agy"][2] == uninstall.HOME / ".gemini" / "config"


def test_agy_mcp_install_and_uninstall(tmp_path: Path):
    mcp_def = {
        "command": "uv",
        "args": [
            "run",
            "--script",
            str(REPO_ROOT / "mcp" / "web_search_mcp.py"),
            "--config",
            str(tmp_path / "web.toml"),
        ],
    }
    install._write_mcp_to_harness("agy", tmp_path, "web", mcp_def)

    mcp_config = tmp_path / "mcp_config.json"
    assert mcp_config.is_file()
    data = json.loads(mcp_config.read_text(encoding="utf-8"))
    assert data["mcpServers"]["web"] == mcp_def

    uninstall.uninstall_mcps("agy", tmp_path, dry_run=False)
    updated = json.loads(mcp_config.read_text(encoding="utf-8"))
    assert "web" not in updated["mcpServers"]


def test_agy_prompt_symlink_install_and_uninstall(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt="": "1")
    install.install_prompt("agy", tmp_path, dry_run=False)

    agents_md = tmp_path / "AGENTS.md"
    assert agents_md.is_symlink()
    assert agents_md.resolve() == (REPO_ROOT / "prompts" / "coding-rules.md").resolve()

    uninstall.uninstall_prompts("agy", tmp_path, dry_run=False)
    assert not agents_md.exists() and not agents_md.is_symlink()
