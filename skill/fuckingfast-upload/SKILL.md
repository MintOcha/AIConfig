---
name: fuckingfast-upload
description: Upload files to fuckingfast.net and manage FuckingFast account files and directories. Use when Codex needs to publish a local artifact and return a download link, choose a storage location, attach a note, inspect account storage, or create, rename, move, annotate, or delete remote items.
---

# FuckingFast Upload

Use `scripts/fuckingfast.py` for deterministic API calls. Never print, store, or commit account IDs; read authenticated credentials from `FUCKINGFAST_ACCOUNT_ID`.

## Upload

Run an anonymous upload unless the user requests account storage:

```bash
python scripts/fuckingfast.py upload /absolute/path/to/file
```

For authenticated uploads, set `FUCKINGFAST_ACCOUNT_ID` in the command environment and add `--parent-id ID` when needed. Use `--location-id ID` for a storage location and `--note TEXT` for a download-page note.

After uploading, report the URL returned by the server. Verify it with a read-only HEAD request when practical. Do not upload an incomplete or stale build artifact; confirm the producing process completed and inspect the file timestamp first.

## File manager

Use these subcommands as needed:

- `locations`
- `account`
- `ls [DIRECTORY_ID]`
- `mkdir PARENT_ID NAME`
- `rename ITEM_ID NAME`
- `move ITEM_ID PARENT_ID`
- `note FILE_ID TEXT`
- `delete DIRECTORY_ID --yes`

Require explicit user authorization before `delete`. Account and file-manager calls require `FUCKINGFAST_ACCOUNT_ID`.
