#!/usr/bin/env python3
"""Capture non-secret OMP agent settings in this repository."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = Path.home() / ".omp" / "agent"
DEFAULT_DESTINATION = REPO_ROOT / "config" / "omp"
SETTINGS_FILES = ("config.yml", "keybindings.yml", "keybindings.json")
SENSITIVE_KEY = re.compile(
    r"(?i)(?:^|[^a-z0-9])(?:api[_-]?key|access[_-]?token|auth[_-]?token|"
    r"client[_-]?secret|password|credential|secret)(?:[^a-z0-9]|$)"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Copy allowlisted OMP settings into AIConfig without secrets or state."
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help=f"OMP agent directory (default: {DEFAULT_SOURCE})",
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=DEFAULT_DESTINATION,
        help=f"tracked settings directory (default: {DEFAULT_DESTINATION})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and report changes without writing them",
    )
    return parser.parse_args()


def sensitive_keys(path: Path, content: str) -> list[str]:
    keys: list[str] = []
    if path.suffix == ".json":
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSON in {path}: {error}") from error

        def visit(value: object) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if SENSITIVE_KEY.search(str(key)):
                        keys.append(str(key))
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(parsed)
        return keys

    for line in content.splitlines():
        match = re.match(r"^\s*([A-Za-z0-9_.-]+)\s*[:=]", line)
        if match and SENSITIVE_KEY.search(match.group(1)):
            keys.append(match.group(1))
    return keys


def load_settings(source: Path) -> dict[str, str]:
    if not source.is_dir():
        raise ValueError(f"OMP agent directory not found: {source}")

    settings: dict[str, str] = {}
    for name in SETTINGS_FILES:
        path = source / name
        if not path.exists():
            continue
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"settings path must be a regular file: {path}")
        content = path.read_text(encoding="utf-8")
        found = sensitive_keys(path, content)
        if found:
            raise ValueError(
                f"refusing {path}: sensitive key(s) found: {', '.join(sorted(set(found)))}"
            )
        settings[name] = content

    if not settings:
        raise ValueError(
            f"no allowlisted settings found in {source}; expected one of: "
            + ", ".join(SETTINGS_FILES)
        )
    return settings


def sync(settings: dict[str, str], destination: Path, dry_run: bool) -> None:
    present = set(settings)
    for name in SETTINGS_FILES:
        action = "copy" if name in present else "remove stale"
        if name in present or (destination / name).exists():
            print(f"{action}: {name}")

    if dry_run:
        return

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        for name, content in settings.items():
            path = temporary / name
            path.write_text(content, encoding="utf-8")
            path.chmod(0o644)
        if destination.exists():
            shutil.rmtree(destination)
        temporary.replace(destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def main() -> int:
    args = parse_args()
    try:
        settings = load_settings(args.source.expanduser().resolve())
        sync(settings, args.destination.expanduser().resolve(), args.dry_run)
    except (OSError, UnicodeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
