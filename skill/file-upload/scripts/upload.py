#!/usr/bin/env python3
"""
Unified File Upload Dispatcher:
- Routes images, SVGs, and videos to E-Z.Host (if configured).
- Supports batch uploads and collation into high-res grids (--grid / --collate).
- Routes to FuckingFast if:
    1. User requests temporary files (--temp).
    2. File is personal/private (--personal).
    3. File is large (--large or size > 100MB).
    4. File is non-media (archives, datasets, binaries, documents, code).
    5. E-Z.Host is unconfigured.
"""

import sys
import os
import glob
import math
import argparse
import subprocess
import tempfile
import time
import tomllib
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
EZHOST_SCRIPT = os.path.join(SCRIPT_DIR, "ezhost.py")
FUCKINGFAST_SCRIPT = os.path.join(SCRIPT_DIR, "fuckingfast.py")

MEDIA_EXTS = {
    # Images
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico", ".tiff", ".avif",
    # Videos
    ".mp4", ".webm", ".mov", ".mkv", ".avi"
}

IMAGE_EXTS = {
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".avif"
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

def collate_images_to_grid(
    image_paths: list[str],
    cols: int | None = None,
    title: str | None = None,
    with_labels: bool = True,
    save_path: str | None = None,
) -> str:
    """Collates multiple images into a high-res grid image."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise RuntimeError("Pillow is required for --grid. Run 'pip install Pillow'.")

    images = []
    for p in image_paths:
        try:
            im = Image.open(p).convert("RGBA")
            images.append((os.path.basename(p), im))
        except Exception as e:
            print(f"Warning: could not open image {p}: {e}", file=sys.stderr)

    if not images:
        raise ValueError("No valid images could be opened for grid collation.")

    num_imgs = len(images)
    if cols is None or cols <= 0:
        if num_imgs <= 2:
            cols = num_imgs
        elif num_imgs <= 4:
            cols = 2
        elif num_imgs <= 9:
            cols = 3
        else:
            cols = 4

    rows = math.ceil(num_imgs / cols)
    max_w = max(im.width for _, im in images)
    max_h = max(im.height for _, im in images)

    pad = 24
    label_h = 36 if with_labels else 0
    title_h = 60 if title else 0

    grid_w = cols * max_w + (cols + 1) * pad
    grid_h = title_h + rows * (max_h + label_h) + (rows + 1) * pad

    # Sleek dark background
    canvas = Image.new("RGBA", (grid_w, grid_h), (24, 24, 27, 255))
    draw = ImageDraw.Draw(canvas)

    # Fonts
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    if not os.path.exists(font_path):
        font_path = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"
    
    font = None
    title_font = None
    if os.path.exists(font_path):
        try:
            font = ImageFont.truetype(font_path, 18)
            title_font = ImageFont.truetype(font_path, 30)
        except Exception:
            pass
    if font is None:
        font = ImageFont.load_default()
        title_font = font

    # Draw Title banner
    if title:
        draw.text((pad, pad // 2 + 10), title, font=title_font, fill=(244, 244, 245, 255))

    y_start = pad + title_h
    for i, (name, im) in enumerate(images):
        c = i % cols
        r = i // cols
        x = pad + c * (max_w + pad)
        y = y_start + r * (max_h + label_h + pad)

        if with_labels:
            clean_label = name
            for prefix in ["src_ReplicatedStorage_Aircraft_", "src_StarterGui_"]:
                if clean_label.startswith(prefix):
                    clean_label = clean_label[len(prefix):]
            for suffix in [".build.luau_sheet.png", ".build.luau_gui.png", ".png"]:
                if clean_label.endswith(suffix):
                    clean_label = clean_label[:-len(suffix)]
            draw.text((x + 8, y + 6), clean_label, font=font, fill=(212, 212, 216, 255))

        offset_x = (max_w - im.width) // 2
        offset_y = (max_h - im.height) // 2
        canvas.paste(im, (x + offset_x, y + label_h + offset_y), im)

    out_file = save_path
    if not out_file:
        fd, out_file = tempfile.mkstemp(prefix="grid_", suffix=".png")
        os.close(fd)

    canvas.convert("RGB").save(out_file, "PNG", optimize=True)
    return out_file

def expand_paths(inputs: list[str]) -> list[str]:
    expanded = []
    for item in inputs:
        if any(char in item for char in ["*", "?", "["]):
            matched = glob.glob(item)
            if matched:
                expanded.extend(matched)
            else:
                expanded.append(item)
        else:
            expanded.append(item)
    return expanded

def main():
    parser = argparse.ArgumentParser(description="Unified File Uploader (E-Z.Host & FuckingFast)")
    parser.add_argument("files", nargs="+", help="Path(s) or glob pattern(s) to file(s) to upload")
    parser.add_argument("-p", "--provider", choices=["auto", "ezhost", "fuckingfast"], default="auto",
                        help="Upload provider (default: auto)")
    parser.add_argument("--grid", "--collate", action="store_true",
                        help="Collate multiple images into a high-res grid before upload")
    parser.add_argument("--cols", type=int, default=None,
                        help="Number of columns for --grid (default: auto)")
    parser.add_argument("--title", type=str, default=None,
                        help="Header title for --grid")
    parser.add_argument("--no-labels", action="store_true",
                        help="Disable tile labels in --grid")
    parser.add_argument("--save-grid", type=str, default=None,
                        help="Save the generated grid image locally to this path")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="Delay between batch uploads in seconds (default: 1.0)")
    parser.add_argument("--temp", action="store_true", help="Mark as temporary file -> routes to FuckingFast")
    parser.add_argument("--personal", action="store_true", help="Mark as personal/private file -> routes to FuckingFast")
    parser.add_argument("--large", action="store_true", help="Mark as large file -> routes to FuckingFast")
    parser.add_argument("--json", action="store_true", help="Output JSON response (ezhost)")
    args, unknown = parser.parse_known_args()

    files = expand_paths(args.files)
    valid_files = [f for f in files if os.path.isfile(f)]
    if not valid_files:
        print(f"Error: No valid files found in: {args.files}", file=sys.stderr)
        sys.exit(1)

    # 1. Collate into grid if requested
    if args.grid or getattr(args, "collate", False):
        if len(valid_files) < 2:
            print("Note: --grid specified with only 1 file; uploading directly.")
        else:
            print(f"Collating {len(valid_files)} images into high-resolution grid...")
            grid_path = collate_images_to_grid(
                valid_files,
                cols=args.cols,
                title=args.title,
                with_labels=not args.no_labels,
                save_path=args.save_grid
            )
            print(f"Grid created: {grid_path} ({os.path.getsize(grid_path) / (1024*1024):.2f} MB)")
            valid_files = [grid_path]

    # 2. Upload file(s)
    uploaded = []
    has_error = False

    for i, file_path in enumerate(valid_files):
        if i > 0 and args.delay > 0:
            time.sleep(args.delay)

        file_size = os.path.getsize(file_path)
        is_large = args.large or (file_size > LARGE_FILE_THRESHOLD_BYTES)
        provider = args.provider

        if provider == "auto":
            ext = os.path.splitext(file_path)[1].lower()
            if args.temp or args.personal or is_large:
                provider = "fuckingfast"
            elif is_ezhost_configured() and ext in MEDIA_EXTS:
                provider = "ezhost"
            else:
                provider = "fuckingfast"

        if provider == "ezhost":
            cmd = [sys.executable, EZHOST_SCRIPT, file_path]
            if args.json:
                cmd.append("--json")
            cmd.extend(unknown)
            res = subprocess.run(cmd)
            if res.returncode != 0:
                print(f"E-Z.Host upload failed for {file_path}; falling back to FuckingFast...", file=sys.stderr)
                cmd = [sys.executable, FUCKINGFAST_SCRIPT, "upload", file_path]
                cmd.extend(unknown)
                res = subprocess.run(cmd)
            if res.returncode != 0:
                has_error = True
            else:
                uploaded.append(file_path)
        else:
            cmd = [sys.executable, FUCKINGFAST_SCRIPT, "upload", file_path]
            cmd.extend(unknown)
            res = subprocess.run(cmd)
            if res.returncode != 0:
                has_error = True
            else:
                uploaded.append(file_path)

    if has_error:
        sys.exit(1)

if __name__ == "__main__":
    main()
