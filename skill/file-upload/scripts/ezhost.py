#!/usr/bin/env python3
"""
E-Z.Host minimal upload client.
Uploads images and files to e-z.host API.
"""

import sys
import os
import argparse
import json
import tomllib
import mimetypes
import time
import urllib.request
import urllib.error
def get_config():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(script_dir)
    config_paths = [
        os.path.join(parent_dir, "config.toml"),
        os.path.join(script_dir, "config.toml"),
        os.path.expanduser("~/.config/ezhost/config.toml")
    ]
    for p in config_paths:
        if os.path.exists(p):
            with open(p, "rb") as f:
                data = tomllib.load(f)
                return data.get("ezhost", data)
    key = os.environ.get("EZHOST_KEY")
    if key:
        return {"key": key, "api_url": os.environ.get("EZHOST_API_URL", "https://api.e-z.host/files")}
    raise FileNotFoundError(f"config.toml not found in: {config_paths} and EZHOST_KEY not set")

def upload_file(file_path, max_retries=5, initial_wait=2.0):
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    config = get_config()
    api_key = config.get("key")
    api_url = config.get("api_url", "https://api.e-z.host/files")

    if not api_key:
        raise ValueError("API key missing in ezhost configuration.")

    filename = os.path.basename(file_path)
    mime_type, _ = mimetypes.guess_type(file_path)
    if not mime_type:
        mime_type = "application/octet-stream"

    with open(file_path, "rb") as f:
        file_bytes = f.read()

    boundary = "----EzHostUploadBoundary" + os.urandom(16).hex()
    
    body = bytearray()
    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8"))
    body.extend(f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"))
    body.extend(file_bytes)
    body.extend(f"\r\n--{boundary}--\r\n".encode("utf-8"))

    req = urllib.request.Request(
        api_url,
        data=bytes(body),
        headers={
            "key": api_key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "ezhost-cli/1.0"
        },
        method="POST"
    )

    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries:
                retry_after = e.headers.get("Retry-After") if hasattr(e, "headers") else None
                wait_time = float(retry_after) if retry_after else (initial_wait * (2 ** attempt))
                print(f"Rate limited (429) uploading {filename}. Waiting {wait_time:.1f}s (retry {attempt+1}/{max_retries})...", file=sys.stderr)
                time.sleep(wait_time)
                continue
            err_msg = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"HTTP Error {e.code}: {err_msg}")
def main():
    parser = argparse.ArgumentParser(description="Upload file(s) to E-Z.Host")
    parser.add_argument("files", nargs="+", help="Path(s) to file(s) to upload")
    parser.add_argument("--json", action="store_true", help="Output raw JSON response")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between batch uploads (seconds, default: 1.0)")
    args = parser.parse_args()

    results = []
    has_error = False
    for i, file_path in enumerate(args.files):
        if i > 0 and args.delay > 0:
            time.sleep(args.delay)
        try:
            res = upload_file(file_path)
            results.append((file_path, res))
            if not args.json:
                image_url = res.get("imageUrl") or res.get("url")
                raw_url = res.get("rawUrl")
                del_url = res.get("deletionUrl")
                if len(args.files) == 1:
                    print(f"URL:      {image_url}")
                    if raw_url:
                        print(f"Raw URL:  {raw_url}")
                    if del_url:
                        print(f"Delete:   {del_url}")
                else:
                    print(f"[{os.path.basename(file_path)}] {image_url}")
        except Exception as e:
            has_error = True
            print(f"Upload failed for {file_path}: {e}", file=sys.stderr)

    if args.json:
        if len(results) == 1:
            print(json.dumps(results[0][1], indent=2))
        else:
            print(json.dumps([{"file": f, "response": r} for f, r in results], indent=2))

    if has_error:
        sys.exit(1)
if __name__ == "__main__":
    main()
