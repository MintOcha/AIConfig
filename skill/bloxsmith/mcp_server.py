"""FastMCP server implementation for Bloxsmith (formerly RbxMCP2).

Wraps build, test, view, cloud asset fetch, toolbox search, catalog search, avatar, and upload.
Supports stdio transport (default) or sse.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image

REPO = Path(os.path.abspath(__file__)).parent
sys.path.insert(0, str(REPO))

from main import current_project, ensure_lune, home, load_config, runtime_repo, save_config
from tools.cloud import (
    avatar_details,
    catalog_search,
    get_default_user_id,
    publish_model,
    pull_asset,
    toolbox_details,
    toolbox_search,
    upload_asset,
)


def create_server(name: str = "RbxMCP") -> FastMCP:
    server = FastMCP(name)

    @server.tool()
    def status() -> dict:
        """Show selected Roblox project, cache folder, and Lune executable status."""
        cfg = load_config()
        lune_bin = ensure_lune()
        return {
            "project": cfg.get("project"),
            "cache": str(home()),
            "lune_ok": lune_bin.exists(),
            "lune_path": str(lune_bin),
        }

    @server.tool()
    def set_project(project_dir: str) -> dict:
        """Select the default Roblox project directory (must contain a src/ folder)."""
        path = Path(project_dir).resolve()
        if not (path / "src").is_dir():
            raise ValueError(f"{path} has no src/ folder")
        cfg = load_config()
        cfg["project"] = str(path)
        cfg["announced"] = str(path)
        save_config(cfg)
        return {"project": str(path), "name": path.name}

    @server.tool()
    def build(target: str = "", project_dir: str = "") -> dict:
        """Compile the Roblox project to dist/game.rbxl, or a specific src/ path to dist/<target>.rbxm."""
        proj = Path(project_dir).resolve() if project_dir else current_project()
        rrepo = runtime_repo()
        cmd = [str(ensure_lune()), "run", str(rrepo / "core" / "build_main.luau"), str(proj).replace("\\", "/")]
        if target:
            cmd.append(target)
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(home()))
        return {
            "success": proc.returncode == 0,
            "exit_code": proc.returncode,
            "output": (proc.stdout + proc.stderr).strip(),
        }

    @server.tool()
    def test(filter: str = "", project_dir: str = "") -> dict:
        """Run headless Luau tests (tests/*.test.luau) and return PASS/FAIL results."""
        proj = Path(project_dir).resolve() if project_dir else current_project()
        rrepo = runtime_repo()
        cmd = [str(ensure_lune()), "run", str(rrepo / "tester" / "run.luau"), str(proj).replace("\\", "/")]
        if filter:
            cmd.append(filter)
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(home()))
        return {
            "success": proc.returncode == 0,
            "exit_code": proc.returncode,
            "output": (proc.stdout + proc.stderr).strip(),
        }

    @server.tool()
    def view(target: str = "", angles: str = "", cframe: str = "", fov: float = 0, project_dir: str = "") -> list:
        """Render 3D world contact sheets, GUI desktop/phone previews, or .anim pose sheets."""
        proj = Path(project_dir).resolve() if project_dir else current_project()
        cmd = [sys.executable, str(REPO / "main.py"), "--project", str(proj), "view"]
        if target:
            cmd.append(target)
        if angles:
            cmd.append(f"--angles={angles}")
        if cframe:
            cmd.append(f"--cframe={cframe}")
        if fov > 0:
            cmd.append(f"--fov={fov}")
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(home()))
        out_text = (proc.stdout + proc.stderr).strip()

        results: list = [out_text]
        for line in out_text.splitlines():
            stripped = line.strip()
            if stripped.endswith(".png") or ".png (" in stripped:
                png_path = Path(stripped.split(" (")[0].strip())
                if png_path.is_file():
                    results.append(Image(data=png_path.read_bytes(), format="png"))
        return results

    @server.tool()
    def toolbox(query: str, category: str = "Model", limit: int = 10) -> list[dict]:
        """Search Roblox Creator Store / Toolbox assets."""
        return toolbox_search(query=query, category=category, limit=limit)

    @server.tool()
    def toolbox_asset(asset_id: int | str) -> dict:
        """Get Creator Store details for a specific asset ID."""
        return toolbox_details(asset_id)

    @server.tool()
    def catalog(query: str, category: str = "All", limit: int = 10) -> list[dict]:
        """Search Roblox Avatar Catalog items."""
        return catalog_search(query=query, category=category, limit=limit)

    @server.tool()
    def avatar(user_id: int | str = "") -> dict:
        """Inspect a Roblox user's avatar rig, scales, and equipped assets."""
        uid = user_id or get_default_user_id()
        if not uid:
            raise ValueError("Provide user_id or log into Roblox Studio")
        return avatar_details(uid)

    @server.tool()
    def pull(asset_id: int | str, output: str = "", project_dir: str = "") -> dict:
        """Download an asset from Roblox and save as .rbxm in assets/."""
        proj = Path(project_dir).resolve() if project_dir else current_project()
        out_path = (proj / output) if output else (proj / "assets" / f"asset_{asset_id}.rbxm")
        saved = pull_asset(asset_id, out_path)
        return {"asset_id": str(asset_id), "path": str(saved), "size_bytes": saved.stat().st_size}

    @server.tool()
    def upload(file_path: str, name: str = "", creator_id: str = "", creator_type: str = "user", asset_type: str = "", project_dir: str = "") -> dict:
        """Upload a local file (or .build.luau) to Roblox Open Cloud Assets API."""
        proj = Path(project_dir).resolve() if project_dir else current_project()
        rrepo = runtime_repo()
        target_path = Path(file_path) if Path(file_path).is_absolute() else (proj / file_path)
        if str(target_path).endswith(".build.luau"):
            rel_path = str(target_path.relative_to(proj)).replace("\\", "/")
            subprocess.run([str(ensure_lune()), "run", str(rrepo / "core" / "build_main.luau"), str(proj).replace("\\", "/"), rel_path], check=True, cwd=str(home()))
            clean_name = rel_path.rstrip("/").removesuffix(".build.luau").replace("/", "_")
            target_path = proj / "dist" / f"{clean_name}.rbxm"
        return upload_asset(
            target_path,
            creator_id=creator_id or None,
            creator_type=creator_type,
            asset_type=asset_type or None,
            name=name or None,
        )

    @server.tool()
    def publish(asset_id: int | str) -> dict:
        """Publish an existing model asset to Creator Store for free distribution."""
        return publish_model(asset_id)

    return server


if __name__ == "__main__":
    server = create_server()
    server.run(transport="stdio")
