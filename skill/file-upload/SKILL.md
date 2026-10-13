---
name: file-upload
description: Upload files (routing across Catbox, E-Z.Host, FuckingFast), view file details/links, and download from a link or id using scripts/upload.py.
---

# Unified File Upload

One CLI: `scripts/upload.py`. It routes uploads across a provider table
(`config.toml`), and also views and downloads files.

## Setup

None required. On first run `config.example.toml` is copied to `config.toml`
(git-ignored) automatically. Fill in provider keys there:

- `[providers.ezhost]` — set `key` (or export `EZHOST_KEY`); used for images <=10MB
  and videos <=100MB.
- `[providers.fuckingfast]` — anonymous by default; export `FUCKINGFAST_ACCOUNT_ID`
  for account features. `mirrors` lists failover hosts for view/download.
- `[providers.catbox]` — permanent hosting, <=200MB, no key needed.

## Upload

Prints the share URL (add `--json` for full JSON):

```bash
python scripts/upload.py /path/to/file.zip          # routes automatically
python scripts/upload.py upload /path/to/file.zip --permanent   # Catbox (<=200MB)
python scripts/upload.py upload file.zip --provider fuckingfast
python scripts/upload.py upload img.png --provider ezhost
python scripts/upload.py upload secret.zip --encrypt             # password-protected zip
```

A bare file path defaults to `upload`. Routing picks `catbox` with `--permanent`
if the file is <=200MB (otherwise warns `permanent ignored due to file size too large`
and uses FuckingFast), `ezhost` for small images/videos with a key, else FuckingFast.

Before uploading anything, delete personal info from it first: home paths,
usernames, emails, tokens, API keys, internal hostnames. There is no flag for
this — inspect the file and strip or regenerate anything identifying.

### Batch upload & grid

Multiple files and globs upload in one run (1s pacing between uploads;
rate-limit 429s are retried with backoff automatically):

```bash
python scripts/upload.py upload out1.png out2.png out3.png
python scripts/upload.py upload "renders/*.png" --delay 1.5
```

`--grid` (alias `--collate`) stitches the images into one labeled
high-resolution grid PNG before uploading, for presenting many screenshots as
a single link:

```bash
python scripts/upload.py upload "renders/*.png" --grid --title "Aircraft Fleet"
python scripts/upload.py upload a.png b.png c.png --grid --cols 3 --save-grid /tmp/grid.png
python scripts/upload.py upload "renders/*.png" --grid --no-labels
```

## View

Id or full link, any mirror; returns name, size, downloads/views, hashes, note,
and a fresh direct `downloadUrl`:

```bash
python scripts/upload.py view imhnttk9fbm4
python scripts/upload.py view https://fuckingfast.net/imhnttk9fbm4 --json
```

## Download

Resolves the id/link and saves it. Uses `aria2c` with 15 connections by default,
falling back to a built-in threaded downloader if aria2c is missing:

```bash
python scripts/upload.py download imhnttk9fbm4
python scripts/upload.py download https://fuckingfast.net/IMHNTTK9FBM4 -o out.7z -c 8
```

## Account management

Requires `FUCKINGFAST_ACCOUNT_ID`:

```bash
python scripts/upload.py ls [DIRECTORY_ID]
python scripts/upload.py mkdir PARENT_ID NAME
python scripts/upload.py rename ITEM_ID NAME
python scripts/upload.py note ITEM_ID TEXT
python scripts/upload.py delete DIRECTORY_ID --yes
```
