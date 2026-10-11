"""Authentication and credentials for Roblox APIs."""
import json
import os
from pathlib import Path


def auth_paths() -> list[Path]:
    paths = []
    # Bloxsmith (rbxmcp) cache config/auth
    if os.environ.get("RBXMCP_HOME"):
        paths.append(Path(os.environ["RBXMCP_HOME"]) / "auth.json")
    elif os.environ.get("LOCALAPPDATA"):
        paths.append(Path(os.environ["LOCALAPPDATA"]) / "rbxmcp" / "auth.json")
    else:
        paths.append(Path.home() / ".cache" / "rbxmcp" / "auth.json")
    # RoMCP fallback
    paths.append(Path.home() / ".romcp" / "auth.json")
    return paths


def load_credentials() -> dict:
    for path in auth_paths():
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    return {}


def api_key() -> str | None:
    key = os.environ.get("ROBLOX_API_KEY")
    if key and key.strip():
        return key.strip()
    creds = load_credentials()
    key = creds.get("api_key")
    if key and isinstance(key, str) and key.strip():
        return key.strip()
    return None


def creator_info() -> tuple[str | None, str]:
    creator_id = os.environ.get("ROBLOX_CREATOR_ID")
    creator_type = os.environ.get("ROBLOX_CREATOR_TYPE", "user")
    if not creator_id:
        creds = load_credentials()
        creator_id = str(creds.get("creator_id") or creds.get("creatorId") or "") or None
        creator_type = str(creds.get("creator_type") or creds.get("creatorType") or "user")
    return creator_id, creator_type
