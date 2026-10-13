#!/usr/bin/env python3
"""Unified file upload/view/download CLI backed by a provider table."""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import glob
import html as html_mod
import json
import math
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR.parent / "config.toml"
EXAMPLE_PATH = SCRIPT_DIR.parent / "config.example.toml"

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
HTML_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

DEFAULT_MIRRORS = [
    "https://fuckingfast.net",
    "https://bzzhr.to",
    "https://fuckingfast.co",
    "https://buzzheavier.com",
    "https://bzzhr.co",
]

CATBOX_MAX_BYTES = 200 * 1024 * 1024
CATBOX_API = "https://catbox.moe/user/api.php"

EZ_HOST_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp", ".bmp", ".ico"}
EZ_HOST_VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".avi"}
EZ_HOST_IMAGE_MAX = 10 * 1024 * 1024
EZ_HOST_VIDEO_MAX = 100 * 1024 * 1024
EZ_HOST_DEFAULT_API = "https://api.e-z.host/files"

FF_UPLOAD = "https://w.fuckingfast.net"
FF_UPLOAD_FALLBACK = "https://w.buzzheavier.com"
FF_API = "https://fuckingfast.net/api"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        if EXAMPLE_PATH.exists():
            shutil.copyfile(EXAMPLE_PATH, CONFIG_PATH)
        else:
            return {"providers": {}}
    with open(CONFIG_PATH, "rb") as f:
        data = tomllib.load(f)
    data.setdefault("providers", {})
    return data


CONFIG = load_config()
PROVIDERS = CONFIG["providers"]
FF_CFG = PROVIDERS.get("fuckingfast", {})
EZ_CFG = PROVIDERS.get("ezhost", {})
CB_CFG = PROVIDERS.get("catbox", {})
MIRRORS = FF_CFG.get("mirrors") or DEFAULT_MIRRORS
API = FF_CFG.get("api_url", FF_API)


def credential(args: argparse.Namespace | None = None, required: bool = True) -> str | None:
    value = None
    if args is not None:
        value = getattr(args, "account_id", None)
    value = value or os.environ.get("FUCKINGFAST_ACCOUNT_ID")
    if required and not value:
        raise SystemExit("Set FUCKINGFAST_ACCOUNT_ID for authenticated operations.")
    return value


def retry_429(do_request, what: str, max_retries: int = 5, initial_wait: float = 2.0):
    """Run do_request(), retrying on HTTP 429 with exponential backoff."""
    for attempt in range(max_retries + 1):
        try:
            return do_request()
        except HTTPError as error:
            if error.code != 429 or attempt >= max_retries:
                raise
            retry_after = error.headers.get("Retry-After") if error.headers else None
            try:
                wait = float(retry_after) if retry_after else initial_wait * (2 ** attempt)
            except (TypeError, ValueError):
                wait = initial_wait * (2 ** attempt)
            print(
                f"Rate limited (429) on {what}. Waiting {wait:.1f}s "
                f"(retry {attempt + 1}/{max_retries})...",
                file=sys.stderr,
            )
            time.sleep(wait)


def api_request(method: str, url: str, account_id: str | None = None, data=None, content_type=None):
    headers = {"Accept": "application/json, text/plain, */*", "User-Agent": UA}
    if account_id:
        headers["Authorization"] = f"Bearer {account_id}"
    if content_type:
        headers["Content-Type"] = content_type
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with retry_429(lambda: urlopen(req), url) as response:
            body = response.read().decode("utf-8", errors="replace")
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {error.code}: {detail or error.reason}") from None
    except URLError as error:
        raise SystemExit(f"Request failed: {error.reason}") from None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body.strip()


def json_body(value: dict) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


# ---------------------------------------------------------------- providers


def ezhost_ok(ext: str, size: int) -> bool:
    if not EZ_CFG.get("enabled", True):
        return False
    if not EZ_CFG.get("key") or "YOUR_API_KEY" in EZ_CFG.get("key", ""):
        if not os.environ.get("EZHOST_KEY"):
            return False
    if ext in EZ_HOST_IMAGE_EXTS:
        return size <= int(EZ_CFG.get("image_max_size_mb", 10)) * 1024 * 1024
    if ext in EZ_HOST_VIDEO_EXTS:
        return size <= int(EZ_CFG.get("video_max_size_mb", 100)) * 1024 * 1024
    return False


def choose_provider(args: argparse.Namespace, path: Path, size: int) -> str:
    ext = path.suffix.lower()
    explicit = getattr(args, "provider", None)
    if explicit and explicit != "auto":
        if explicit == "ezhost" and not ezhost_ok(ext, size):
            raise SystemExit("ezhost cannot host this file (extension/size limits).")
        return explicit
    if getattr(args, "permanent", False):
        if size <= CATBOX_MAX_BYTES:
            return "catbox"
        print(
            f"warning: permanent ignored due to file size too large "
            f"({size / (1024 * 1024):.1f} MB > 200 MB)",
            file=sys.stderr,
        )
        return "fuckingfast"
    if getattr(args, "temp", False) or getattr(args, "large", False) or size > 100 * 1024 * 1024:
        return "fuckingfast"
    if ezhost_ok(ext, size):
        return "ezhost"
    return "fuckingfast"


def upload_catbox(path: Path) -> dict:
    boundary = secrets.token_hex(16)
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"reqtype\"\r\n\r\nfileupload\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"fileToUpload\"; filename=\"{path.name}\"\r\n"
        f"Content-Type: {mime}\r\n\r\n".encode(),
        path.read_bytes(),
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    req = Request(CATBOX_API, data=b"".join(parts), headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "User-Agent": UA,
    }, method="POST")
    try:
        with retry_429(lambda: urlopen(req, timeout=120), "catbox") as resp:
            raw = resp.read().decode("utf-8", errors="replace").strip()
    except HTTPError as e:
        raise SystemExit(f"Catbox HTTP {e.code}: {e.read().decode(errors='replace')}") from None
    if not raw.startswith("http"):
        raise SystemExit(f"Catbox upload error: {raw}")
    return {"code": 201, "provider": "catbox", "url": raw, "data": {"name": path.name}}


def upload_ezhost(path: Path) -> dict:
    key = EZ_CFG.get("key") or os.environ.get("EZHOST_KEY")
    if not key or "YOUR_API_KEY" in key:
        raise SystemExit("E-Z.Host key not configured.")
    api_url = EZ_CFG.get("api_url", EZ_HOST_DEFAULT_API)
    boundary = secrets.token_hex(16)
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
        f"Content-Type: {mime}\r\n\r\n".encode(),
        path.read_bytes(),
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    req = Request(api_url, data=b"".join(parts), headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "key": key,
        "User-Agent": UA,
    }, method="POST")
    try:
        with retry_429(lambda: urlopen(req, timeout=120), path.name) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except HTTPError as e:
        raise SystemExit(f"E-Z.Host HTTP {e.code}: {e.read().decode(errors='replace')}") from None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"code": 201, "provider": "ezhost", "url": body.strip()}


def upload_fuckingfast(path: Path, args: argparse.Namespace) -> dict:
    encryption_password = None
    temp_archive = None
    upload_path = path

    if getattr(args, "encrypt", False):
        tool = shutil.which("7zz") or shutil.which("7z")
        if not tool:
            raise SystemExit("7-Zip (7zz or 7z) binary required for --encrypt.")
        encryption_password = args.password or secrets.token_urlsafe(12)
        fd, tmp = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        temp_archive = Path(tmp)
        temp_archive.unlink(missing_ok=True)
        res = subprocess.run(
            [tool, "a", f"-p{encryption_password}", str(temp_archive), str(path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        if res.returncode != 0:
            temp_archive.unlink(missing_ok=True)
            raise SystemExit(f"Archive encryption failed: {res.stderr.strip()}")
        upload_path = temp_archive

    try:
        name = args.name or (f"{path.name}.zip" if getattr(args, "encrypt", False) else path.name)
        if len(name) > 500:
            raise SystemExit("Remote file name exceeds 500 characters.")
        account_id = credential(args, required=bool(args.parent_id))
        prefix = f"/{quote(args.parent_id, safe='')}" if args.parent_id else ""
        query = {}
        if args.location_id:
            query["locationId"] = args.location_id
        if args.note is not None:
            query["note"] = base64.b64encode(args.note.encode()).decode()
        base = FF_CFG.get("upload_url", FF_UPLOAD)
        url = f"{base}{prefix}/{quote(name, safe='')}"
        if query:
            url += "?" + urlencode(query)
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        data = upload_path.read_bytes()
        try:
            resp = api_request("PUT", url, account_id, data, content_type)
        except SystemExit:
            fallback = FF_CFG.get("fallback_upload_url")
            if not fallback:
                raise
            url = f"{fallback}{prefix}/{quote(name, safe='')}"
            if query:
                url += "?" + urlencode(query)
            resp = api_request("PUT", url, account_id, data, content_type)
        if encryption_password:
            if isinstance(resp, dict):
                resp["encryptionPassword"] = encryption_password
        return resp
    finally:
        if temp_archive and temp_archive.exists():
            temp_archive.unlink(missing_ok=True)


def expand_paths(patterns: list[str]) -> list[Path]:
    paths: list[Path] = []
    for item in patterns:
        if any(ch in item for ch in "*?["):
            matched = sorted(glob.glob(item))
            if not matched:
                raise SystemExit(f"No files match pattern: {item}")
            paths.extend(matched)
        else:
            paths.append(item)
    resolved = [Path(p).expanduser().resolve() for p in paths]
    missing = [p for p in resolved if not p.is_file()]
    if missing:
        raise SystemExit(f"File not found: {missing[0]}")
    return resolved


def collate_images_to_grid(
    image_paths: list[Path],
    cols: int | None = None,
    title: str | None = None,
    with_labels: bool = True,
    save_path: str | None = None,
) -> str:
    """Stitch images into one labeled high-resolution grid PNG."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as e:
        raise SystemExit("Pillow is required for --grid. Run 'pip install Pillow'.") from e

    images: list[tuple[str, "Image.Image"]] = []
    for p in image_paths:
        try:
            images.append((p.stem, Image.open(p).convert("RGBA")))
        except Exception as e:
            print(f"Warning: could not open image {p}: {e}", file=sys.stderr)
    if not images:
        raise SystemExit("No valid images could be opened for grid collation.")

    n = len(images)
    if not cols or cols <= 0:
        cols = 1 if n <= 2 else 2 if n <= 4 else 3 if n <= 9 else 4
    rows = math.ceil(n / cols)
    max_w = max(im.width for _, im in images)
    max_h = max(im.height for _, im in images)

    pad = 24
    label_h = 36 if with_labels else 0
    title_h = 60 if title else 0
    grid_w = cols * max_w + (cols + 1) * pad
    grid_h = title_h + rows * (max_h + label_h) + (rows + 1) * pad

    canvas = Image.new("RGBA", (grid_w, grid_h), (24, 24, 27, 255))
    draw = ImageDraw.Draw(canvas)

    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    if not os.path.exists(font_path):
        font_path = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"
    font = title_font = None
    if os.path.exists(font_path):
        try:
            font = ImageFont.truetype(font_path, 18)
            title_font = ImageFont.truetype(font_path, 30)
        except OSError:
            pass
    if font is None:
        font = title_font = ImageFont.load_default()

    if title:
        draw.text((pad, pad // 2 + 10), title, font=title_font, fill=(244, 244, 245, 255))

    y_start = pad + title_h
    for i, (name, im) in enumerate(images):
        x = pad + (i % cols) * (max_w + pad)
        y = y_start + (i // cols) * (max_h + label_h + pad)
        if with_labels:
            draw.text((x + 8, y + 6), name, font=font, fill=(212, 212, 216, 255))
        canvas.paste(im, (x + (max_w - im.width) // 2, y + label_h + (max_h - im.height) // 2), im)

    out_file = save_path
    if not out_file:
        fd, out_file = tempfile.mkstemp(prefix="grid_", suffix=".png")
        os.close(fd)
    canvas.convert("RGB").save(out_file, "PNG", compress_level=6)
    return out_file


def upload_one(path: Path, args: argparse.Namespace) -> dict:
    size = path.stat().st_size
    provider = choose_provider(args, path, size)
    if provider == "catbox":
        resp = upload_catbox(path)
    elif provider == "ezhost":
        try:
            resp = upload_ezhost(path)
        except SystemExit as e:
            print(f"E-Z.Host upload failed ({e}); falling back to FuckingFast...", file=sys.stderr)
            resp = upload_fuckingfast(path, args)
    else:
        resp = upload_fuckingfast(path, args)

    # Annotate with canonical share link where we know the id.
    if isinstance(resp, dict) and resp.get("provider") != "catbox":
        data = resp.get("data") or {}
        if data.get("id"):
            resp["url"] = f"https://fuckingfast.net/{data['id']}"
    return resp


def run_upload(args: argparse.Namespace) -> dict | list[dict]:
    paths = expand_paths(args.files)

    generated_grid: Path | None = None
    if getattr(args, "grid", False):
        if len(paths) < 2:
            print("Note: --grid specified with only 1 file; uploading directly.", file=sys.stderr)
        else:
            print(f"Collating {len(paths)} images into a high-resolution grid...", file=sys.stderr)
            grid_path = collate_images_to_grid(
                paths,
                cols=args.cols,
                title=args.title,
                with_labels=not args.no_labels,
                save_path=args.save_grid,
            )
            print(f"Grid created: {grid_path}", file=sys.stderr)
            generated_grid = None if args.save_grid else Path(grid_path)
            paths = [Path(grid_path)]

    results: list[dict] = []
    failed = False
    try:
        for i, path in enumerate(paths):
            if i and args.delay > 0:
                time.sleep(args.delay)
            try:
                results.append(upload_one(path, args))
            except SystemExit as e:
                failed = True
                print(f"Upload failed for {path}: {e}", file=sys.stderr)
    finally:
        if generated_grid:
            generated_grid.unlink(missing_ok=True)

    if failed:
        raise SystemExit(1)
    return results[0] if len(results) == 1 else results


# ---------------------------------------------------------------- view/download


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def extract_id(item: str) -> str:
    s = item.strip()
    if s.startswith(("http://", "https://")):
        parts = urlparse(s).path.strip("/").split("/")
        return parts[0] if parts else ""
    return s.strip("/")


def fetch_direct_link(page_url: str, dl_path: str, method: str = "GET") -> str | None:
    parsed = urlparse(page_url)
    host = f"{parsed.scheme}://{parsed.netloc}"
    target = f"{host}{dl_path}" if dl_path.startswith("/") else dl_path
    headers = {
        "User-Agent": UA,
        "HX-Request": "true",
        "HX-Current-URL": page_url,
        "Referer": page_url,
    }
    data = None
    if method == "POST":
        headers["Origin"] = host
        data = b""
    opener = build_opener(NoRedirect)
    req = Request(target, data=data, headers=headers, method=method)
    try:
        resp = opener.open(req, timeout=15)
        return resp.headers.get("hx-redirect") or resp.headers.get("location")
    except HTTPError as e:
        return e.headers.get("hx-redirect") or e.headers.get("location")
    except Exception:
        return None


BAD_PAGE_MARKERS = (
    "Just a moment...",
    "File Not Found Or Deleted",
    "Whatever lived here has returned to the void",
    "<title>Not Found</title>",
)


def parse_page(body: str) -> dict | None:
    if any(m in body for m in BAD_PAGE_MARKERS):
        return None
    details: dict = {}
    m = re.search(r'<p class="file-name">([^<]+)</p>', body) or re.search(
        r'<span class="text-xl">([^<]+)</span>', body
    ) or re.search(r"<title>([^<]+)</title>", body)
    if m:
        name = html_mod.unescape(m.group(1).strip())
        if name not in ("Not Found", "Fuckingfast", "BUZZHEAVIER", "BZZHR", "Error"):
            details["name"] = name
    m = re.search(r"(\d+)\s+downloads\s+(?:&middot;|·)\s+(\d+)\s+views", body, re.I)
    if m:
        details["downloads"] = int(m.group(1))
        details["views"] = int(m.group(2))
    else:
        m = re.search(r"Size:\s*([^|]+)\|\s*Downloads:\s*(\d+)", body, re.I)
        if m:
            details["size"] = m.group(1).strip()
            details["downloads"] = int(m.group(2))
    m = re.search(r'<span class="size">([^<]+)</span>', body)
    if m:
        details["size"] = m.group(1).strip()
    m = re.search(r"SHA-256:.*?<code[^>]*>([a-f0-9]{64})</code>", body, re.I | re.S)
    if m:
        details["sha256"] = m.group(1)
    m = re.search(r"MD5:.*?<code[^>]*>([a-f0-9]{32})</code>", body, re.I | re.S)
    if m:
        details["md5"] = m.group(1)
    m = re.search(r"<p><strong>Note:</strong>\s*(.*?)</p>", body, re.I | re.S)
    if m:
        details["note"] = html_mod.unescape(m.group(1).strip())
    m = re.search(r'hx-get="(/[^"]+/download(?:\?t=[^"&]+)?)"', body)
    if m:
        details["_dl"] = (m.group(1), "GET")
        return details
    m = re.search(r'hx-post="(/f/[^/]+/go)"', body)
    if m:
        details["_dl"] = (m.group(1), "POST")
        return details
    return details if details else None


def resolve_file(item: str) -> dict:
    file_id = extract_id(item)
    if not file_id:
        raise SystemExit(f"Could not extract file id from '{item}'")
    if item.startswith(("http://", "https://")) and urlparse(item).netloc.endswith("catbox.moe"):
        raise SystemExit("Catbox links have no file metadata; download the link directly.")

    mirrors = list(MIRRORS)
    if item.startswith(("http://", "https://")):
        given = f"{urlparse(item).scheme}://{urlparse(item).netloc}"
        if given in mirrors:
            mirrors.remove(given)
            mirrors.insert(0, given)

    last_error: Exception | None = None
    for mirror in mirrors:
        page_url = f"{mirror}/{file_id}"
        try:
            with urlopen(Request(page_url, headers=HTML_HEADERS), timeout=15) as resp:
                if resp.status != 200:
                    continue
                body = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            last_error = e
            continue
        details = parse_page(body)
        if details is None:
            continue
        details.pop("_dl", None)
        dl_path, method = (None, None)
        for probe in (
            (r'hx-get="(/[^"]+/download(?:\?t=[^"&]+)?)"', "GET"),
            (r'hx-post="(/f/[^/]+/go)"', "POST"),
        ):
            mm = re.search(probe[0], body)
            if mm:
                dl_path, method = mm.group(1), probe[1]
                break
        direct = None
        if dl_path:
            direct = fetch_direct_link(page_url, dl_path, method)
        if not direct:
            direct = fetch_direct_link(page_url, f"/{file_id}/download", "HEAD")
        details.update({"id": file_id, "url": f"https://fuckingfast.net/{file_id}", "mirror": mirror})
        details["downloadUrl"] = direct
        return details
    raise SystemExit(f"File '{file_id}' not found on any mirror (last error: {last_error})")


def multi_download(url: str, output: Path, connections: int = 15) -> Path:
    aria = shutil.which("aria2c")
    if aria:
        cmd = [
            aria, "-x", str(connections), "-s", str(connections), "-k", "1M",
            "-d", str(output.parent), "-o", output.name, url,
        ]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if res.returncode == 0:
            return output
        print(f"aria2c failed ({res.stderr.strip()}); falling back to built-in downloader...", file=sys.stderr)

    req = Request(url, headers={"User-Agent": UA, "Range": "bytes=0-0"})
    supports_range, total = False, None
    try:
        with urlopen(req, timeout=15) as resp:
            cr = resp.headers.get("Content-Range")
            if resp.status == 206 and cr:
                supports_range = True
                total = int(cr.split("/")[-1])
            elif resp.headers.get("Content-Length"):
                total = int(resp.headers.get("Content-Length"))
    except Exception:
        pass

    if not supports_range or not total or total < 1024 * 1024 or connections <= 1:
        req = Request(url, headers={"User-Agent": UA})
        with urlopen(req, timeout=60) as resp, open(output, "wb") as f:
            while chunk := resp.read(64 * 1024):
                f.write(chunk)
        return output

    with open(output, "wb") as f:
        f.truncate(total)
    chunk_size = total // connections
    ranges = [
        (i * chunk_size, total - 1 if i == connections - 1 else (i + 1) * chunk_size - 1)
        for i in range(connections)
    ]

    def fetch(part: tuple[int, int]) -> int:
        start, end = part
        r = Request(url, headers={"User-Agent": UA, "Range": f"bytes={start}-{end}"})
        with urlopen(r, timeout=60) as resp:
            data = resp.read()
        with open(output, "r+b") as f:
            f.seek(start)
            f.write(data)
        return len(data)

    with concurrent.futures.ThreadPoolExecutor(max_workers=connections) as pool:
        for _ in pool.map(fetch, ranges):
            pass
    return output


def run_view(args: argparse.Namespace) -> dict:
    details = resolve_file(args.target)
    if getattr(args, "check", False):
        url = details.get("downloadUrl")
        if url:
            req = Request(url, headers={"User-Agent": UA, "Range": "bytes=0-0"})
            try:
                with urlopen(req, timeout=15) as resp:
                    details["checkStatus"] = resp.status
            except Exception as e:
                details["checkStatus"] = str(e)
    return details


def run_download(args: argparse.Namespace) -> str:
    target = args.target
    direct = None
    name = None
    if target.startswith(("http://", "https://")):
        host = urlparse(target).netloc.lower()
        if not any(host == urlparse(m).netloc for m in MIRRORS):
            # Already a direct link (Catbox, dl.*/ts.* CDN, ...): fetch as-is.
            direct = target
            name = Path(urlparse(target).path).name or None

    if direct is None:
        details = resolve_file(target)
        direct = details.get("downloadUrl")
        name = details.get("name") or details.get("id")
    if not direct:
        raise SystemExit(f"Could not resolve a direct download link for '{target}'")

    out = Path(args.output).expanduser() if args.output else Path.cwd()
    if out.exists() and out.is_dir() or (args.output and out.suffix == ""):
        out = out / (name or "download")
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
    multi_download(direct, out, connections=args.connections)
    print(f"Downloaded: {out} ({out.stat().st_size} bytes)")
    return str(out)


# ---------------------------------------------------------------- main


def run_response(result, args):
    if result is None:
        return
    if isinstance(result, str):
        print(result)
        return
    if args.command == "upload" and isinstance(result, list) and not getattr(args, "json", False):
        for item in result:
            run_response(item, args)
        return
    if getattr(args, "json", False) or not isinstance(result, dict):
        print(json.dumps(result, indent=2))
        return
    if args.command == "upload":
        url = result.get("url")
        if not url:
            data = result.get("data") or {}
            if data.get("id"):
                url = f"https://fuckingfast.net/{data['id']}"
        if url:
            print(url)
            enc = result.get("encryptionPassword")
            if enc:
                print(f"Encryption Password: {enc}")
        else:
            print(json.dumps(result, indent=2))
        return
    print(json.dumps(result, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Upload, view, and download files via configured providers.")
    parser.add_argument("--account-id", help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", help="Output JSON")
    subs = parser.add_subparsers(dest="command")

    p = subs.add_parser("upload", help="Upload one or more files")
    p.add_argument("files", nargs="+", help="File path(s) or glob pattern(s)")
    p.add_argument("--provider", choices=["auto", "ezhost", "catbox", "fuckingfast"], default="auto")
    p.add_argument("--permanent", action="store_true", help="Permanent hosting (Catbox, <=200MB)")
    p.add_argument("--temp", action="store_true")
    p.add_argument("--large", action="store_true")
    p.add_argument("--name")
    p.add_argument("--parent-id")
    p.add_argument("--location-id")
    p.add_argument("--note")
    p.add_argument("--encrypt", action="store_true")
    p.add_argument("--password")
    p.add_argument("--delay", type=float, default=1.0, help="Seconds between batch uploads (default 1.0)")
    p.add_argument("--grid", "--collate", action="store_true",
                   help="Stitch multiple images into one labeled grid before upload")
    p.add_argument("--cols", type=int, help="Grid column count (default: auto)")
    p.add_argument("--title", help="Grid header title")
    p.add_argument("--no-labels", action="store_true", help="Hide tile filename labels in grid")
    p.add_argument("--save-grid", help="Also save the generated grid image to this path")

    p = subs.add_parser("view", help="View file details from an id or link")
    p.add_argument("target")
    p.add_argument("--check", action="store_true", help="HEAD-check the direct download link")

    p = subs.add_parser("download", help="Download a file from an id or link")
    p.add_argument("target")
    p.add_argument("-o", "--output", help="Output file or directory")
    p.add_argument("-c", "--connections", type=int, default=15, help="Parallel connections (default 15)")

    subs.add_parser("locations")
    subs.add_parser("account")
    p = subs.add_parser("ls")
    p.add_argument("directory_id", nargs="?")
    p = subs.add_parser("mkdir")
    p.add_argument("parent_id")
    p.add_argument("name")
    p = subs.add_parser("rename")
    p.add_argument("item_id")
    p.add_argument("name")
    p = subs.add_parser("move")
    p.add_argument("item_id")
    p.add_argument("parent_id")
    p = subs.add_parser("note")
    p.add_argument("item_id")
    p.add_argument("text")
    p = subs.add_parser("delete")
    p.add_argument("directory_id")
    p.add_argument("--yes", action="store_true")

    # Bare positional file path -> default to upload (agent-friendly).
    argv = sys.argv[1:]
    known = {s for s in ("upload", "view", "download", "locations", "account", "ls",
                         "mkdir", "rename", "move", "note", "delete")}
    if argv and argv[0] not in known and not argv[0].startswith("-"):
        argv.insert(0, "upload")

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return

    if args.command == "upload":
        result = run_upload(args)
    elif args.command == "view":
        result = run_view(args)
    elif args.command == "download":
        result = run_download(args)
    elif args.command == "locations":
        result = api_request("GET", f"{API}/locations", credential(args, False))
    elif args.command == "account":
        result = api_request("GET", f"{API}/account", credential(args))
    elif args.command == "ls":
        suffix = f"/{quote(args.directory_id, safe='')}" if args.directory_id else ""
        result = api_request("GET", f"{API}/fs{suffix}", credential(args))
    elif args.command == "mkdir":
        result = api_request("POST", f"{API}/fs/{quote(args.parent_id, safe='')}", credential(args), json_body({"name": args.name}), "application/json")
    elif args.command == "rename":
        result = api_request("PATCH", f"{API}/fs/{quote(args.item_id, safe='')}", credential(args), json_body({"name": args.name}), "application/json")
    elif args.command == "move":
        result = api_request("PUT", f"{API}/fs/{quote(args.item_id, safe='')}", credential(args), json_body({"parentId": args.parent_id}), "application/json")
    elif args.command == "note":
        result = api_request("PUT", f"{API}/fs/{quote(args.item_id, safe='')}", credential(args), json_body({"note": args.text}), "application/json")
    else:
        if not args.yes:
            raise SystemExit("Refusing deletion without --yes.")
        result = api_request("DELETE", f"{API}/fs/{quote(args.directory_id, safe='')}", credential(args))

    run_response(result, args)


if __name__ == "__main__":
    main()
