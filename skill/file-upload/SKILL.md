---
name: file-upload
description: Upload files, images, SVGs, documents, and archives using E-Z.Host (images, SVGs, hosted direct URLs) or FuckingFast (binaries, archives, datasets, general file downloads).
---

# Unified File Upload

Provides two upload backends for publishing local artifacts and returning URLs:
1. **E-Z.Host**: For images, SVGs, screenshots, visual charts, and direct media hosting.
2. **FuckingFast**: For general files, datasets, ZIP/TAR archives, binaries, and remote file management.

## Setup & Credentials

- **E-Z.Host**: Place your API key in `config.toml` (git-ignored):
  ```toml
  [ezhost]
  api_url = "https://api.e-z.host/files"
  key = "YOUR_API_KEY"
  ```
  Or export `EZHOST_KEY`.
- **FuckingFast**: Anonymous uploads require no setup. For account storage, export `FUCKINGFAST_ACCOUNT_ID`.

## Usage

### Auto Dispatcher (`scripts/upload.py`)
Auto-routes images and SVGs to E-Z.Host, and general files/archives to FuckingFast:
```bash
python scripts/upload.py /path/to/image.png
python scripts/upload.py /path/to/dataset.zip
```

### Batch Upload & High-Res Grid Collation (`--grid` / `--collate`)
To avoid rate limits or present multiple screenshots/sheets cleanly, pass multiple files or globs with `--grid` (or `--collate`) to stitch them at full native resolution into a labeled grid before uploading:
```bash
# Collate multiple images into a single high-res labeled grid and upload
python scripts/upload.py --grid --title "Aircraft Fleet" /path/to/renders/*.png

# Customize grid columns, save locally, or hide tile labels
python scripts/upload.py --grid --cols 3 --save-grid /tmp/grid.png img1.png img2.png img3.png

# Batch upload individual files with rate-limit retry & pacing
python scripts/upload.py --delay 1.5 img1.png img2.png img3.png
```

Explicit provider flags:
```bash
python scripts/upload.py --provider ezhost /path/to/file.svg
python scripts/upload.py --provider fuckingfast /path/to/file.csv
```

- **E-Z.Host direct**:
  ```bash
  python scripts/ezhost.py /path/to/chart.svg
  ```
- **FuckingFast direct & management**:
  ```bash
  python scripts/fuckingfast.py upload /path/to/file.zip
  python scripts/fuckingfast.py ls
  ```
