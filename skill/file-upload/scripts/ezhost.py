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

def upload_file(file_path):
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
            "User-Agent": "ShareX/17.0.0"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP Error {e.code}: {err_msg}")

def main():
    parser = argparse.ArgumentParser(description="Upload file to E-Z.Host")
    parser.add_argument("file", help="Path to file to upload")
    parser.add_argument("--json", action="store_true", help="Output raw JSON response")
    args = parser.parse_args()

    try:
        res = upload_file(args.file)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            image_url = res.get("imageUrl") or res.get("url")
            raw_url = res.get("rawUrl")
            del_url = res.get("deletionUrl")
            print(f"URL:      {image_url}")
            if raw_url:
                print(f"Raw URL:  {raw_url}")
            if del_url:
                print(f"Delete:   {del_url}")
    except Exception as e:
        print(f"Upload failed: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
