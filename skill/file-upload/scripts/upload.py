#!/usr/bin/env python3
"""
Unified File Upload Dispatcher:
- Routes images, SVGs, and videos to E-Z.Host (if configured).
- Routes to FuckingFast if:
    1. User requests temporary files (--temp).
    2. File is personal/private (--personal).
    3. File is large (--large or size > 100MB).
    4. File is non-media (archives, datasets, binaries, documents, code).
    5. E-Z.Host is unconfigured.
"""

import sys
import os
import argparse
import subprocess
import tomllib

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
EZHOST_SCRIPT = os.path.join(SCRIPT_DIR, "ezhost.py")
FUCKINGFAST_SCRIPT = os.path.join(SCRIPT_DIR, "fuckingfast.py")

MEDIA_EXTS = {
    # Images
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico", ".tiff", ".avif",
    # Videos
    ".mp4", ".webm", ".mov", ".mkv", ".avi"
}

# 100 MB threshold
LARGE_FILE_THRESHOLD_BYTES = 100 * 1024 * 1024

def is_ezhost_configured():
    if os.environ.get("EZHOST_KEY"):
        return True
    parent_dir = os.path.dirname(SCRIPT_DIR)
    config_paths = [
        os.path.join(parent_dir, "config.toml"),
        os.path.join(SCRIPT_DIR, "config.toml"),
        os.path.expanduser("~/.config/ezhost/config.toml")
    ]
    for p in config_paths:
        if os.path.exists(p):
            try:
                with open(p, "rb") as f:
                    cfg = tomllib.load(f)
                    data = cfg.get("ezhost", cfg)
                    if data.get("key") and "YOUR_API_KEY" not in data.get("key"):
                        return True
            except Exception:
                pass
    return False

def main():
    parser = argparse.ArgumentParser(description="Unified File Uploader (E-Z.Host & FuckingFast)")
    parser.add_argument("file", help="Path to file to upload")
    parser.add_argument("-p", "--provider", choices=["auto", "ezhost", "fuckingfast"], default="auto",
                        help="Upload provider (default: auto)")
    parser.add_argument("--temp", action="store_true", help="Mark as temporary file -> routes to FuckingFast")
    parser.add_argument("--personal", action="store_true", help="Mark as personal/private file -> routes to FuckingFast")
    parser.add_argument("--large", action="store_true", help="Mark as large file -> routes to FuckingFast")
    parser.add_argument("--json", action="store_true", help="Output JSON response (ezhost)")
    args, unknown = parser.parse_known_args()

    if not os.path.isfile(args.file):
        print(f"Error: File not found: {args.file}", file=sys.stderr)
        sys.exit(1)

    file_size = os.path.getsize(args.file)
    is_large = args.large or (file_size > LARGE_FILE_THRESHOLD_BYTES)

    provider = args.provider
    if provider == "auto":
        ext = os.path.splitext(args.file)[1].lower()
        # Direct to FuckingFast if temporary, personal, large, or non-media
        if args.temp or args.personal or is_large:
            provider = "fuckingfast"
        elif is_ezhost_configured() and ext in MEDIA_EXTS:
            provider = "ezhost"
        else:
            provider = "fuckingfast"

    if provider == "ezhost":
        cmd = [sys.executable, EZHOST_SCRIPT, args.file]
        if args.json:
            cmd.append("--json")
        cmd.extend(unknown)
        res = subprocess.run(cmd)
        if res.returncode != 0:
            print("E-Z.Host upload failed; falling back to FuckingFast...", file=sys.stderr)
            cmd = [sys.executable, FUCKINGFAST_SCRIPT, "upload", args.file]
            cmd.extend(unknown)
            res = subprocess.run(cmd)
        sys.exit(res.returncode)
    else:
        cmd = [sys.executable, FUCKINGFAST_SCRIPT, "upload", args.file]
        cmd.extend(unknown)
        res = subprocess.run(cmd)
        sys.exit(res.returncode)

if __name__ == "__main__":
    main()
