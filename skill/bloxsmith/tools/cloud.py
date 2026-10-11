"""Roblox cloud integration for Bloxsmith (formerly RbxMCP2):
Toolbox, Creator Store, Catalog, Avatar, asset delivery (pulling .rbxm), and Open Cloud upload.
"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import gzip
import http.client
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

AUTH_PATH = Path.home() / ".romcp" / "auth.json"

UPLOAD_FORMATS = {
    "Audio": {".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".wav": "audio/wav", ".flac": "audio/flac"},
    "Image": {".png": "image/png", ".jpeg": "image/jpeg", ".jpg": "image/jpeg", ".bmp": "image/bmp", ".tga": "image/tga"},
    "Decal": {".png": "image/png", ".jpeg": "image/jpeg", ".jpg": "image/jpeg", ".bmp": "image/bmp", ".tga": "image/tga"},
    "Model": {".fbx": "model/fbx", ".gltf": "model/gltf+json", ".glb": "model/gltf-binary", ".rbxm": "model/x-rbxm", ".rbxmx": "model/x-rbxm"},
    "Animation": {".rbxm": "model/x-rbxm", ".rbxmx": "model/x-rbxm"},
    "Mesh": {".mesh": "model/x-file-mesh-data"},
}
UPLOAD_LIMIT = 20 * 1024 * 1024


def _load_auth_json() -> dict:
    if AUTH_PATH.exists():
        try:
            return json.loads(AUTH_PATH.read_text(encoding="utf-8-sig"))
        except Exception:
            return {}
    return {}


def get_api_key() -> str | None:
    key = os.environ.get("ROBLOX_API_KEY")
    if key and key.strip():
        return key.strip()
    return _load_auth_json().get("api_key")


def get_cookie() -> str | None:
    """Resolve .ROBLOSECURITY token from environment, config, or local Studio credentials."""
    env_cookie = os.environ.get("ROBLOSECURITY") or os.environ.get("ROBLOX_COOKIE")
    if env_cookie and env_cookie.strip():
        val = env_cookie.strip()
        if not val.startswith(".ROBLOSECURITY="):
            val = f".ROBLOSECURITY={val}"
        return val

    auth = _load_auth_json()
    if auth.get("cookie"):
        val = str(auth["cookie"]).strip()
        if not val.startswith(".ROBLOSECURITY="):
            val = f".ROBLOSECURITY={val}"
        return val

    # On Windows, decrypt RobloxCookies.dat from LocalStorage using DPAPI
    if sys.platform == "win32":
        try:
            cookie_file = Path(os.environ.get("LOCALAPPDATA", "")) / "Roblox" / "LocalStorage" / "RobloxCookies.dat"
            if cookie_file.exists():
                data = json.loads(cookie_file.read_text())["CookiesData"]
                raw = base64.b64decode(data)

                class DATA_BLOB(ctypes.Structure):
                    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]

                in_blob = DATA_BLOB(len(raw), ctypes.cast(ctypes.c_char_p(raw), ctypes.POINTER(ctypes.c_char)))
                out_blob = DATA_BLOB()
                crypt32 = ctypes.windll.crypt32
                if crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)):
                    decrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData).decode("utf-8", errors="ignore")
                    ctypes.windll.kernel32.LocalFree(out_blob.pbData)
                    for line in decrypted.splitlines():
                        tokens = line.split("\t")
                        for i, token in enumerate(tokens):
                            if token == ".ROBLOSECURITY" and i + 1 < len(tokens):
                                return f".ROBLOSECURITY={tokens[i + 1].strip()}"
        except Exception:
            pass
    return None


def get_default_user_id() -> int | None:
    """Get the currently logged in Studio user ID."""
    auth = _load_auth_json()
    if auth.get("creator_id"):
        try:
            return int(auth["creator_id"])
        except ValueError:
            pass

    if sys.platform == "win32":
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Roblox\RobloxStudio\LoggedInUsersStore\https:\www.roblox.com")
            v = winreg.QueryValueEx(k, "users")[0]
            # Format might be concatenated JSON objects like {"841398204":...};{"4562206609":...}
            for part in v.split(";"):
                part = part.strip()
                if part:
                    try:
                        d = json.loads(part)
                        for uid in d.keys():
                            if uid.isdigit():
                                return int(uid)
                    except Exception:
                        pass
        except Exception:
            pass
    return None


def toolbox_search(query: str = "", category: str = "Model", limit: int = 10) -> list[dict]:
    """Search Roblox Creator Store / Toolbox using the v2 endpoint."""
    params = {
        "searchCategoryType": category,
        "maxPageSize": max(1, min(limit, 30)),
    }
    if query.strip():
        params["query"] = query.strip()

    url = "https://apis.roblox.com/toolbox-service/v2/assets:search?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Roblox/WinInet"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Toolbox search HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}") from e

    results = []
    for item in data.get("creatorStoreAssets", []):
        asset = item.get("asset", {})
        creator = item.get("creator", {})
        results.append({
            "id": asset.get("id"),
            "name": asset.get("name", "Unnamed"),
            "type": asset.get("typeId"),
            "creator": creator.get("name", "Unknown"),
            "creator_id": creator.get("id"),
            "creator_type": creator.get("type"),
        })
    return results


def toolbox_details(asset_id: int | str) -> dict:
    """Retrieve Creator Store asset metadata."""
    url = f"https://apis.roblox.com/toolbox-service/v2/assets/{asset_id}"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Roblox/WinInet"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Toolbox details HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}") from e


def catalog_search(query: str = "", category: str = "All", limit: int = 10) -> list[dict]:
    """Search Roblox Avatar Catalog."""
    params = {
        "Limit": max(1, min(limit, 30)),
    }
    if query.strip():
        params["Keyword"] = query.strip()
    if category != "All":
        params["Category"] = category

    url = "https://catalog.roblox.com/v1/search/items/details?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Roblox/WinInet"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Catalog search HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}") from e

    results = []
    for item in data.get("data", []):
        results.append({
            "id": item.get("id"),
            "name": item.get("name", "Unnamed"),
            "creator": item.get("creatorName", "Unknown"),
            "creator_id": item.get("creatorTargetId"),
            "price": item.get("price"),
            "item_type": item.get("itemType"),
            "asset_type": item.get("assetType"),
        })
    return results


def avatar_details(user_id: int | str) -> dict:
    """Retrieve user avatar rig configuration and currently equipped assets."""
    url = f"https://avatar.roblox.com/v1/users/{user_id}/avatar"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Roblox/WinInet"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Avatar details HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}") from e


def pull_asset(asset_id: int | str, output_path: str | Path) -> Path:
    """Download an asset binary (.rbxm, audio, image) from Roblox and save to disk."""
    out = Path(output_path).resolve()
    cookie = get_cookie()
    headers = {"User-Agent": "Roblox/WinInet"}
    if cookie:
        headers["Cookie"] = cookie

    # Step 1: Query assetdelivery v2
    v2_url = f"https://assetdelivery.roblox.com/v2/assetId/{asset_id}"
    req = urllib.request.Request(v2_url, headers=headers)
    cdn_url = None
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            locations = data.get("locations", [])
            if locations:
                cdn_url = locations[0].get("location")
    except Exception:
        pass

    # Step 2: Fallback to direct assetdelivery v1 if v2 location was not returned
    if not cdn_url:
        cdn_url = f"https://assetdelivery.roblox.com/v1/asset/?id={asset_id}"

    # Step 3: Fetch binary content from CDN
    fetch_req = urllib.request.Request(cdn_url, headers=headers)
    try:
        with urllib.request.urlopen(fetch_req, timeout=30) as resp:
            content = resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Asset delivery HTTP {e.code} for asset {asset_id}: {e.read().decode('utf-8', errors='replace')}") from e

    # Step 4: Decompress if gzipped
    if len(content) >= 2 and content[:2] == b"\x1f\x8b":
        try:
            content = gzip.decompress(content)
        except Exception:
            pass

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(content)
    return out


def upload_asset(
    file_path: str | Path,
    creator_id: int | str | None = None,
    creator_type: str = "user",
    asset_type: str | None = None,
    name: str | None = None,
    description: str = "",
    wait_seconds: float = 60,
) -> dict:
    """Upload a local file or model using Open Cloud Assets API."""
    path = Path(file_path).resolve(strict=True)
    if not path.is_file():
        raise ValueError(f"Upload target {path} is not a regular file")

    if not creator_id:
        creator_id = get_default_user_id()
    if not creator_id or not str(creator_id).isdigit() or int(creator_id) < 1:
        raise ValueError("Specify a valid positive creator_id (or log into Roblox Studio)")

    api_key = get_api_key()
    if not api_key:
        raise ValueError(f"Set ROBLOX_API_KEY environment variable or api_key in {AUTH_PATH}")

    suffix = path.suffix.lower()
    if asset_type is None:
        candidates = [kind for kind, formats in UPLOAD_FORMATS.items() if suffix in formats and kind != "Decal"]
        if len(candidates) != 1:
            raise ValueError(f"Specify --assetType explicitly for {suffix}; choices: {list(UPLOAD_FORMATS.keys())}")
        asset_type = candidates[0]

    if asset_type not in UPLOAD_FORMATS or suffix not in UPLOAD_FORMATS[asset_type]:
        raise ValueError(f"Unsupported format {suffix} for {asset_type}")

    size = path.stat().st_size
    if not 0 < size <= UPLOAD_LIMIT:
        raise ValueError("File must be non-empty and at most 20 MiB")

    boundary = uuid.uuid4().hex
    metadata = {
        "assetType": asset_type,
        "displayName": name or path.stem,
        "description": description,
        "creationContext": {
            "creator": {f"{creator_type}Id": str(creator_id)}
        },
    }

    prefix = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"request\"\r\nContent-Type: application/json\r\n\r\n"
        + json.dumps(metadata)
        + f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"fileContent\"; filename=\"asset{suffix}\"\r\nContent-Type: {UPLOAD_FORMATS[asset_type][suffix]}\r\n\r\n"
    ).encode("utf-8")
    ending = f"\r\n--{boundary}--\r\n".encode("utf-8")

    conn = http.client.HTTPSConnection("apis.roblox.com", timeout=60)
    try:
        conn.putrequest("POST", "/assets/v1/assets")
        headers = {
            "x-api-key": api_key,
            "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(prefix) + size + len(ending)),
        }
        for k, v in headers.items():
            conn.putheader(k, v)
        conn.endheaders()
        conn.send(prefix)

        with path.open("rb") as f:
            while chunk := f.read(65536):
                conn.send(chunk)
        conn.send(ending)

        resp = conn.getresponse()
        body = resp.read()
        if resp.status >= 300:
            raise RuntimeError(f"Upload failed HTTP {resp.status}: {body.decode(errors='replace')}")
        result = json.loads(body.decode("utf-8"))
    finally:
        conn.close()

    # Poll operation status if required
    op_path = result.get("path")
    if op_path and wait_seconds > 0:
        op_id = op_path.removeprefix("operations/")
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline and not result.get("done"):
            time.sleep(2)
            op_req = urllib.request.Request(
                f"https://apis.roblox.com/assets/v1/operations/{op_id}",
                headers={"x-api-key": api_key, "Accept": "application/json"},
            )
            try:
                with urllib.request.urlopen(op_req, timeout=15) as op_resp:
                    result = json.loads(op_resp.read().decode("utf-8"))
            except Exception:
                pass
    return result


def publish_model(asset_id: int | str) -> dict:
    """Distribute an existing model for free in Creator Store."""
    if not str(asset_id).isdigit() or int(asset_id) < 1:
        raise ValueError("asset_id must be a positive integer")

    api_key = get_api_key()
    if not api_key:
        raise ValueError(f"Set ROBLOX_API_KEY environment variable or api_key in {AUTH_PATH}")

    data = {
        "modelAssetId": str(asset_id),
        "published": True,
        "basePrice": {"currencyCode": "USD", "quantity": {"significand": 0, "exponent": 0}},
    }
    req = urllib.request.Request(
        "https://apis.roblox.com/cloud/v2/creator-store-products",
        data=json.dumps(data).encode("utf-8"),
        headers={"x-api-key": api_key, "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Publish model HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}") from e
