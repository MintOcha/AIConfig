#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "fastmcp==3.4.5",
#   "kaggle>=2.2.4",
# ]
# ///
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

# Runtime configuration defaults
WORKSPACE_ROOT = Path.cwd().resolve()
CONFIG_USERNAME: str | None = None
CONFIG_API_TOKEN: str | None = None

app = FastMCP("kaggle")


# ---------------------------------------------------------------------------
# Path & Workspace Resolution
# ---------------------------------------------------------------------------

def get_workspace_root() -> Path:
    return WORKSPACE_ROOT


def get_kaggle_root() -> Path:
    root = get_workspace_root() / "kaggle"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _slugify(value: str) -> str:
    cleaned = value.strip().lower()
    cleaned = re.sub(r"[^a-z0-9]+", "-", cleaned)
    return cleaned.strip("-")


def _sanitize_local_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "", (value or "").strip())
    return cleaned or "notebook"


def _split_kernel_slug(value: str) -> tuple[str | None, str]:
    cleaned = (value or "").strip()
    if "/" not in cleaned:
        return None, cleaned
    owner, slug = cleaned.split("/", 1)
    return (owner.strip() or None), slug.strip()


def _full_slug(row: dict[str, Any]) -> str | None:
    for key in ("ref", "slug", "kernelRef", "id"):
        val = row.get(key)
        if isinstance(val, str) and "/" in val:
            return val.strip()
    owner = row.get("owner") or row.get("owner_slug") or row.get("username")
    slug = row.get("kernel_slug") or row.get("slug")
    if owner and slug:
        return f"{owner}/{slug}"
    return None


def _source_path(notebook: str) -> tuple[str, Path, str]:
    """Resolve notebook name/path relative to workspace root."""
    kaggle_root = get_kaggle_root()
    raw = Path(notebook)
    candidate = raw if raw.is_absolute() else get_workspace_root() / raw

    if candidate.is_dir():
        name = candidate.name
        files = sorted(candidate.glob("*.ipynb")) + sorted(candidate.glob("*.py"))
        preferred = [p for p in files if p.name in ("notebook.ipynb", "notebook.py")]
        path = preferred[0] if preferred else (files[0] if files else None)
        if path is None:
            raise FileNotFoundError(f"No .ipynb or .py source found in directory: {candidate}")
    elif raw.suffix in (".ipynb", ".py"):
        path = candidate
        name = path.parent.name
    else:
        name = notebook.strip().strip("/")
        folder = kaggle_root / name
        if not folder.exists():
            folded = name.casefold()
            matches = [
                p
                for p in kaggle_root.iterdir()
                if p.is_dir() and (p.name.casefold() == folded or _slugify(p.name).casefold() == folded)
            ]
            if matches:
                folder = matches[0]
                name = folder.name
        for filename in ("notebook.ipynb", "notebook.py"):
            path = folder / filename
            if path.exists():
                break
        else:
            matches = sorted(folder.glob("*.ipynb")) + sorted(folder.glob("*.py"))
            path = matches[0] if matches else folder / "notebook.ipynb"

    path = path.resolve()
    if path.suffix == ".ipynb":
        kernel_type = "notebook"
    elif path.suffix == ".py":
        kernel_type = "script"
    else:
        raise ValueError(f"Unsupported Kaggle source type: {path}")
    return name, path, kernel_type


def _resolve_py_source(name: str, explicit: str | None = None) -> Path:
    kaggle_root = get_kaggle_root()
    if explicit:
        p = Path(explicit)
        candidate = p if p.is_absolute() else get_workspace_root() / p
        candidate = candidate.resolve()
        if candidate.exists() and candidate.suffix == ".py":
            return candidate
        folder_candidate = (kaggle_root / name / p).resolve()
        if folder_candidate.exists() and folder_candidate.suffix == ".py":
            return folder_candidate
        raise FileNotFoundError(f"Python source not found: {candidate}")

    folder = kaggle_root / name
    for cand in (
        folder / f"{name.lower()}.py",
        folder / f"{name}.py",
        folder / "notebook.py",
        folder / "train.py",
        folder / "main.py",
    ):
        if cand.exists():
            return cand.resolve()
    py_files = sorted(folder.glob("*.py"))
    if py_files:
        return py_files[0].resolve()
    raise FileNotFoundError(f"No .py source found in: {folder}")


# ---------------------------------------------------------------------------
# Credentials & Execution Environment
# ---------------------------------------------------------------------------

def _load_token() -> str:
    if CONFIG_API_TOKEN:
        return CONFIG_API_TOKEN

    for env_var in ("KAGGLE_MCP_TOKEN", "KAGGLE_API_TOKEN", "KAGGLE_KEY"):
        token = os.environ.get(env_var)
        if token:
            return token.strip()

    # Check ~/.kaggle/access_token
    token_file = Path.home() / ".kaggle" / "access_token"
    if token_file.exists():
        token = token_file.read_text().strip()
        if token:
            return token

    # Check ~/.kaggle/kaggle.json
    kaggle_json = Path.home() / ".kaggle" / "kaggle.json"
    if kaggle_json.exists():
        try:
            data = json.loads(kaggle_json.read_text())
            key = data.get("key") or data.get("token")
            if key:
                return str(key).strip()
        except Exception:
            pass

    # Check Codex config
    for cfg_path in (
        get_workspace_root() / ".codex" / "config.toml",
        Path.home() / ".codex" / "config.toml",
    ):
        if not cfg_path.exists():
            continue
        try:
            data = tomllib.loads(cfg_path.read_text())
            server = data.get("mcp_servers", {}).get("kaggle", {})
            auth = server.get("http_headers", {}).get("Authorization", "")
            if auth.startswith("Bearer "):
                return auth.removeprefix("Bearer ").strip()
        except Exception:
            pass

    raise RuntimeError(
        "Kaggle token not found. Set KAGGLE_API_TOKEN, configure ~/.kaggle/kaggle.json, "
        "or set api_token in kaggle.toml."
    )


def _default_owner() -> str | None:
    if CONFIG_USERNAME:
        return CONFIG_USERNAME

    env_owner = os.environ.get("KAGGLE_USERNAME")
    if env_owner and env_owner != "kaggle":
        return env_owner.strip()

    try:
        from kaggle.api.kaggle_api_extended import KaggleApi

        api = KaggleApi()
        api.authenticate()
        user = api.get_config_value("username")
        if user and user != "kaggle":
            return str(user).strip()
    except Exception:
        pass

    kaggle_json = Path.home() / ".kaggle" / "kaggle.json"
    if kaggle_json.exists():
        try:
            user = json.loads(kaggle_json.read_text()).get("username")
            if user and user != "kaggle":
                return str(user).strip()
        except Exception:
            pass

    return None


def _cli_env() -> dict[str, str]:
    env = os.environ.copy()
    try:
        token = _load_token()
        env.setdefault("KAGGLE_API_TOKEN", token)
        env.setdefault("KAGGLE_KEY", token)
    except Exception:
        pass
    owner = _default_owner()
    if owner:
        env.setdefault("KAGGLE_USERNAME", owner)
    return env


def _resolve_kaggle_cmd() -> list[str]:
    # 1. System PATH
    which = shutil.which("kaggle")
    if which:
        return [which]
    # 2. Virtualenv/uv bin next to python interpreter
    py_bin = Path(sys.executable).parent / "kaggle"
    if py_bin.exists() and py_bin.is_file():
        return [str(py_bin)]
    # 3. Local .venv in workspace root
    local_venv = get_workspace_root() / ".venv" / "bin" / "kaggle"
    if local_venv.exists() and local_venv.is_file():
        return [str(local_venv)]
    # 4. Fallback: run via python entrypoint
    return [sys.executable, "-c", "from kaggle.cli import main; main()"]


def _strip_cli_warnings(text: str) -> str:
    cleaned_lines = [
        line
        for line in (text or "").splitlines()
        if not line.strip().startswith("Warning: Looks like you're using an outdated `kaggle`")
    ]
    return "\n".join(cleaned_lines)


def _run_kaggle(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess[str]:
    cmd = [*_resolve_kaggle_cmd(), *args]
    proc = subprocess.run(
        cmd,
        cwd=get_workspace_root(),
        env=_cli_env(),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=True,
    )
    # Strip annoying version warning banners
    proc.stdout = _strip_cli_warnings(proc.stdout)
    proc.stderr = _strip_cli_warnings(proc.stderr)
    return proc


def _parse_kernel_csv(output: str) -> list[dict[str, str]]:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    header_idx = -1
    for idx, line in enumerate(lines):
        if line.lower().startswith("ref,") or "ref,title" in line.lower():
            header_idx = idx
            break
    if header_idx == -1:
        for idx, line in enumerate(lines):
            if "," in line and not line.lower().startswith("warning"):
                header_idx = idx
                break
    if header_idx == -1:
        return []

    csv_text = "\n".join(lines[header_idx:])
    rows = list(csv.DictReader(csv_text.splitlines()))
    return [
        {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k}
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Accelerator Handling
# ---------------------------------------------------------------------------

def _normalize_accelerator(value: str) -> str:
    normalized = (value or "none").strip()
    key = normalized.lower().replace("_", "-")
    aliases = {
        "nvidia-tesla-t4": "NvidiaTeslaT4",
        "nvidia-t4": "NvidiaTeslaT4",
        "tesla-t4": "NvidiaTeslaT4",
        "t4": "NvidiaTeslaT4",
        "gput4x2": "NvidiaTeslaT4",
        "gput4": "NvidiaTeslaT4",
        "gpu": "NvidiaTeslaT4",
        "nvidiateslat4": "NvidiaTeslaT4",
        "nvidia-tesla-p100": "NvidiaTeslaP100",
        "nvidia-p100": "NvidiaTeslaP100",
        "p100": "NvidiaTeslaP100",
        "nvidia-tesla-v100": "NvidiaTeslaV100",
        "v100": "NvidiaTeslaV100",
        "nvidia-tesla-a100": "NvidiaTeslaA100",
        "a100": "NvidiaTeslaA100",
        "nvidia-l4": "NvidiaL4",
        "l4": "NvidiaL4",
        "nvidia-tesla-t4x2": "NvidiaTeslaT4",
        "nvidia_tesla_t4": "NvidiaTeslaT4",
        "nvidia_tesla_p100": "NvidiaTeslaP100",
        "none": "none",
        "cpu": "none",
        "tpu": "tpu",
        "tpu-v3-8": "tpu",
    }
    return aliases.get(key, normalized)


def _is_gpu_accelerator(value: str) -> bool:
    normalized = _normalize_accelerator(value).lower()
    return normalized.startswith("nvidia")


def _read_notebook_accelerator(path: Path) -> str | None:
    if path.suffix != ".ipynb" or not path.exists():
        return None
    try:
        nb = json.loads(path.read_text())
        acc = nb.get("metadata", {}).get("kaggle", {}).get("accelerator", "")
        if acc and acc.lower() not in ("none", ""):
            return _normalize_accelerator(acc)
    except Exception:
        pass
    return None


def _resolve_accelerator(explicit: str, source_path: Path, cache: dict[str, Any]) -> str:
    normalized = _normalize_accelerator(explicit)
    if normalized.lower() not in ("none", ""):
        return normalized
    nb_acc = _read_notebook_accelerator(source_path)
    if nb_acc:
        return nb_acc
    cached_acc = cache.get("accelerator") or cache.get("machine_shape") or cache.get("enable_gpu")
    if cached_acc:
        if isinstance(cached_acc, bool):
            return "NvidiaTeslaT4" if cached_acc else "none"
        if isinstance(cached_acc, str):
            norm = _normalize_accelerator(cached_acc)
            if norm.lower() not in ("none", ""):
                return norm
    return "none"


# ---------------------------------------------------------------------------
# High-Speed Inlined Run Compression Engine
# ---------------------------------------------------------------------------

IMPORTANT_PATTERNS = re.compile(
    r"(?i)(traceback|exception|\berror\b|failed|out of memory|cuda out of memory|\boomed\b|"
    r"\bloss\b|\bmetric\b|\baccuracy\b|\bauc\b|\bf1\b|\bepoch\b|\bbest\b|\bval_loss\b|\bscore\b)"
)
PROGRESS_PATTERN = re.compile(r"(\d+/\d+|\d+%|\[=+>*-*\]|\+\d+)")


def human_size(size: int) -> str:
    val = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024 or unit == "TB":
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= 1024
    return f"{val:.1f} TB"


def compact_directory_tree(root: Path, max_depth: int = 4, max_files_per_dir: int = 20) -> str:
    lines: list[str] = [f"{root.name}/"]

    def walk(path: Path, prefix: str = "", depth: int = 0) -> None:
        if depth > max_depth:
            lines.append(f"{prefix}[max depth reached]")
            return
        try:
            entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except Exception as err:
            lines.append(f"{prefix}[unreadable: {err}]")
            return

        shown_files = 0
        total_files = sum(1 for e in entries if not e.is_dir())
        count = len(entries)

        for i, entry in enumerate(entries):
            is_last = (i == count - 1)
            branch = "└── " if is_last else "├── "
            next_prefix = prefix + ("    " if is_last else "│   ")

            if entry.is_dir():
                lines.append(f"{prefix}{branch}{entry.name}/")
                walk(entry, next_prefix, depth + 1)
            else:
                if shown_files >= max_files_per_dir:
                    hidden = total_files - shown_files
                    lines.append(f"{prefix}└── ... and {hidden} more files")
                    break
                try:
                    size = human_size(entry.stat().st_size)
                    lines.append(f"{prefix}{branch}{entry.name} ({size})")
                except Exception:
                    lines.append(f"{prefix}{branch}{entry.name}")
                shown_files += 1

    walk(root)
    return "\n".join(lines)


def compress_logs(
    text: str,
    max_head: int = 40,
    max_tail: int = 80,
    max_critical: int = 150,
) -> str:
    """Deduplicates repetitive progress loops and extracts critical error/metric highlights."""
    lines = text.splitlines()
    total = len(lines)
    if total <= max_head + max_tail:
        return "\n".join(lines)

    head = lines[:max_head]
    tail = lines[-max_tail:]
    middle = lines[max_head:-max_tail]

    critical_lines: list[str] = []
    last_sig: str | None = None
    rep_count = 0

    for line in middle:
        sig = PROGRESS_PATTERN.sub("<NUM>", line.strip())
        if sig == last_sig and PROGRESS_PATTERN.search(line):
            rep_count += 1
            continue
        elif rep_count > 0:
            critical_lines.append(f"  [... {rep_count} repetitive progress lines collapsed ...]")
            rep_count = 0

        last_sig = sig
        if IMPORTANT_PATTERNS.search(line) and len(critical_lines) < max_critical:
            critical_lines.append(line)

    if rep_count > 0:
        critical_lines.append(f"  [... {rep_count} repetitive progress lines collapsed ...]")

    result: list[str] = []
    result.extend(head)
    result.append(f"\n--- [MIDDLE SECTION: {len(middle)} lines collapsed; {len(critical_lines)} highlights shown] ---")
    result.extend(critical_lines)
    result.append(f"--- [TAIL SECTION: {len(tail)} lines] ---\n")
    result.extend(tail)
    return "\n".join(result)


def build_run_summary(run_dir: Path) -> str:
    """Produce summary.txt, tree.txt, and logs.compressed.txt for an executed run."""
    output_dir = run_dir / "output"
    tree_text = compact_directory_tree(output_dir) if output_dir.exists() else "[no output directory]"
    (run_dir / "tree.txt").write_text(tree_text)

    # Process logs
    logs_file = run_dir / "logs.txt"
    raw_logs = logs_file.read_text(errors="replace") if logs_file.exists() else ""
    compressed_logs = compress_logs(raw_logs)
    (run_dir / "logs.compressed.txt").write_text(compressed_logs)

    # Find metrics.json
    metrics_str = "[no metrics.json found]"
    for cand in (output_dir / "metrics.json", run_dir / "metrics.json"):
        if cand.exists():
            try:
                metrics_data = json.loads(cand.read_text())
                metrics_str = json.dumps(metrics_data, indent=2)
                break
            except Exception:
                pass

    summary_lines = [
        "# Kaggle Run Summary",
        "",
        "## Directory Tree",
        "```text",
        tree_text,
        "```",
        "",
        "## Metrics",
        "```json",
        metrics_str,
        "```",
        "",
        "## Log Highlights",
        "```text",
        compressed_logs[:1200] + ("..." if len(compressed_logs) > 1200 else ""),
        "```",
    ]
    summary_text = "\n".join(summary_lines)
    (run_dir / "summary.txt").write_text(summary_text)
    return summary_text


# ---------------------------------------------------------------------------
# Metadata & Cache Helpers
# ---------------------------------------------------------------------------

def _kernel_cache_path(name: str) -> Path:
    return get_kaggle_root() / name / "working" / "kaggle_kernel.json"


def _local_metadata_path(source_path: Path) -> Path:
    return source_path.parent / "metadata.json"


def _read_cache(name_or_path: str | Path) -> dict[str, Any]:
    folder = name_or_path if isinstance(name_or_path, Path) else get_kaggle_root() / name_or_path
    if folder.is_file():
        folder = folder.parent
    cache_file = folder / "working" / "kaggle_kernel.json"
    meta_file = folder / "metadata.json"

    data: dict[str, Any] = {}
    if cache_file.exists():
        try:
            data.update(json.loads(cache_file.read_text()))
        except Exception:
            pass
    if meta_file.exists():
        try:
            data.update(json.loads(meta_file.read_text()))
        except Exception:
            pass
    return data


def _write_cache(name: str, data: dict[str, Any]) -> None:
    cache_path = _kernel_cache_path(name)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(data, indent=2, sort_keys=True))


def _write_local_metadata(source_path: Path, data: dict[str, Any]) -> None:
    path = _local_metadata_path(source_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def _clean_status_label(status: str | None) -> str:
    if not status:
        return "UNKNOWN"
    return str(status).replace("KernelWorkerStatus.", "").strip()


def _as_cell_source(text: str) -> list[str]:
    lines = text.splitlines(keepends=True)
    return lines or [""]


# ---------------------------------------------------------------------------
# Core Operations
# ---------------------------------------------------------------------------

def _cli_find_kernel(title: str) -> tuple[bool, dict[str, Any]]:
    try:
        proc = _run_kaggle(
            ["kernels", "list", "--mine", "--search", title, "--page-size", "100", "--sort-by", "relevance", "--csv"],
            timeout=60,
        )
        rows = _parse_kernel_csv(proc.stdout)
        folded = title.casefold()
        for row in rows:
            ref = row.get("ref", "")
            t = row.get("title", "")
            _, slug = _split_kernel_slug(ref)
            if folded in {t.casefold(), slug.casefold(), ref.casefold()}:
                return True, row
        if rows:
            return True, rows[0]
    except Exception:
        pass
    return False, {}


async def _find_kernel(title: str, name: str) -> tuple[bool, dict[str, Any]]:
    cached = _read_cache(name)
    slug = cached.get("slug") or cached.get("id")
    if slug:
        return True, cached

    # Query Kaggle SDK or CLI
    owner = _default_owner()
    if owner:
        try:
            from kaggle.api.kaggle_api_extended import KaggleApi
            from kagglesdk.kernels.types.kernels_api_service import ApiGetKernelRequest

            api = KaggleApi()
            api.authenticate()
            with api.build_kaggle_client() as client:
                req = ApiGetKernelRequest()
                req.user_name = owner
                req.kernel_slug = _slugify(name)
                res = client.kernels.kernels_api_client.get_kernel(req)
                if res and res.metadata:
                    meta = res.metadata.to_dict()
                    return True, meta
        except Exception:
            pass

    return _cli_find_kernel(title)


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@app.tool()
async def save_notebook_file(
    notebook: str,
    run: bool = True,
    private: bool = True,
    internet: bool = True,
    accelerator: str = "none",
    py_source: str | None = None,
    dataset_sources: list[str] | None = None,
    competition_sources: list[str] | None = None,
    model_sources: list[str] | None = None,
) -> str:
    """Save a local Kaggle notebook file, push to Kaggle, and optionally run it.

    Saves the notebook under ./kaggle/<notebook_name>/.
    When py_source is provided (or a .py file exists), syncs the script into notebook.ipynb
    (%%writefile + !python pattern) to avoid DDP/interactive GPU issues.
    """
    name, path, kernel_type = _source_path(notebook)
    cache_pre = _read_cache(name)
    title = name

    if py_source is None:
        try:
            py_path = _resolve_py_source(name, None)
            py_source = py_path.name
        except FileNotFoundError:
            pass

    # Auto-sync .py → .ipynb
    if py_source is not None or kernel_type == "script":
        py_path = _resolve_py_source(name, py_source)
        py_text = py_path.read_text()
        script_name = py_path.name or "train.py"
        nb_acc = _resolve_accelerator(accelerator, path, cache_pre)

        existing_acc = _read_notebook_accelerator(path)
        if existing_acc and nb_acc == "none":
            nb_acc = existing_acc

        folder = get_kaggle_root() / name
        ipynb_path = folder / "notebook.ipynb"
        if ipynb_path.exists():
            try:
                nb = json.loads(ipynb_path.read_text())
            except Exception:
                nb = {}
        else:
            nb = {}

        nb.setdefault("cells", [])
        nb.setdefault("metadata", {})
        nb["metadata"].setdefault("kernelspec", {"display_name": "Python 3", "language": "python", "name": "python3"})
        nb["metadata"].setdefault("language_info", {"name": "python"})
        nb["metadata"].setdefault("kaggle", {})["accelerator"] = nb_acc
        nb["metadata"]["kaggle"]["isInternetEnabled"] = internet
        nb["metadata"]["kaggle"]["language"] = "python"
        nb["metadata"]["kaggle"]["sourceType"] = "notebook"
        nb["nbformat"] = 4
        nb["nbformat_minor"] = 5

        nb["cells"] = [
            {"cell_type": "markdown", "metadata": {}, "source": f"# {title}\n\nAuto-synced from `{script_name}`.\n"},
            {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": _as_cell_source(f"%%writefile {script_name}\n" + py_text)},
            {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": _as_cell_source(f"!python {script_name}")},
        ]
        ipynb_path.write_text(json.dumps(nb, indent=2))
        path = ipynb_path
        kernel_type = "notebook"
    else:
        nb = json.loads(path.read_text())
        nb_acc = _resolve_accelerator(accelerator, path, cache_pre)
        nb.setdefault("metadata", {}).setdefault("kaggle", {})["accelerator"] = nb_acc
        nb["metadata"]["kaggle"]["isInternetEnabled"] = internet
        path.write_text(json.dumps(nb, indent=2))

    existed, kernel = await _find_kernel(title, name)
    owner = _default_owner()
    if not owner:
        raise RuntimeError("Could not determine Kaggle username. Set KAGGLE_USERNAME or configure ~/.kaggle/kaggle.json.")

    ref = _full_slug(kernel) or f"{owner}/{_slugify(name)}"
    push_dir = get_kaggle_root() / name / "working" / "kaggle_push"
    push_dir.mkdir(parents=True, exist_ok=True)
    code_file = "notebook.ipynb" if kernel_type == "notebook" else path.name
    shutil.copy(path, push_dir / code_file)

    acc = _resolve_accelerator(accelerator, path, _read_cache(name))
    datasets = dataset_sources if dataset_sources is not None else list(cache_pre.get("dataset_sources") or [])
    competitions = competition_sources if competition_sources is not None else list(cache_pre.get("competition_sources") or [])
    models = model_sources if model_sources is not None else list(cache_pre.get("model_sources") or [])
    metadata = {
        "id": ref,
        "title": title,
        "code_file": code_file,
        "language": "python",
        "kernel_type": kernel_type,
        "is_private": "true" if private else "false",
        "enable_gpu": "true" if _is_gpu_accelerator(acc) else "false",
        "enable_tpu": "true" if acc.lower() == "tpu" else "false",
        "enable_internet": "true" if internet else "false",
        "dataset_sources": datasets,
        "competition_sources": competitions,
        "kernel_sources": [],
        "model_sources": models,
    }
    (push_dir / "kernel-metadata.json").write_text(json.dumps(metadata, indent=2))

    cmd = ["kernels", "push", "-p", str(push_dir)]
    if acc and acc.lower() not in ("none", "cpu"):
        cmd.extend(["--accelerator", acc])

    result = _run_kaggle(cmd, timeout=600)
    version_match = re.search(r"Kernel version\s+(\d+)\s+successfully pushed", result.stdout, re.I)
    version = int(version_match.group(1)) if version_match else None

    # Update cache and local metadata
    owner_slug, kernel_slug = _split_kernel_slug(ref)
    cache_update = {
        "title": title,
        "slug": ref,
        "id": ref,
        "owner_slug": owner_slug,
        "username": owner_slug,
        "kernel_slug": kernel_slug,
        "source_path": str(path.relative_to(get_workspace_root())),
        "kernel_type": kernel_type,
        "accelerator": acc,
        "internet": internet,
        "private": private,
        "last_pushed_utc": datetime.now(timezone.utc).isoformat(),
        "dataset_sources": datasets,
        "competition_sources": competitions,
        "model_sources": models,
    }
    if version:
        cache_update["last_version_number"] = version
    _write_cache(name, cache_update)
    _write_local_metadata(path, cache_update)

    status_msg = f"Updating existing Kaggle notebook: {title}" if existed else f"Creating new Kaggle notebook: {title}"
    attached = []
    if datasets:
        attached.append(f"datasets: {', '.join(datasets)}")
    if competitions:
        attached.append(f"competitions: {', '.join(competitions)}")
    if models:
        attached.append(f"models: {', '.join(models)}")
    attached_msg = f"\nattached: {'; '.join(attached)}" if attached else ""
    return (
        f"{status_msg}\n"
        f"slug: {ref}\n"
        f"version: {version or 'latest'}\n"
        f"accelerator: {acc}\n"
        f"internet: {internet}"
        f"{attached_msg}\n"
        f"Notebook successfully queued on Kaggle."
    )


@app.tool()
async def sync_py_to_ipynb(notebook: str, py_source: str | None = None) -> str:
    """Convert/sync a local .py script into ./kaggle/<notebook>/notebook.ipynb with %%writefile + !python."""
    name, _, _ = _source_path(notebook)
    py_path = _resolve_py_source(name, py_source)
    folder = get_kaggle_root() / name
    folder.mkdir(parents=True, exist_ok=True)
    ipynb_path = folder / "notebook.ipynb"

    py_text = py_path.read_text()
    script_name = py_path.name or "train.py"

    nb = {
        "cells": [
            {"cell_type": "markdown", "metadata": {}, "source": f"# {name}\n\nAuto-synced from `{script_name}`.\n"},
            {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": _as_cell_source(f"%%writefile {script_name}\n" + py_text)},
            {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": _as_cell_source(f"!python {script_name}")},
        ],
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python"},
            "kaggle": {"accelerator": "none", "dataSources": [], "isInternetEnabled": True, "language": "python", "sourceType": "notebook"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    ipynb_path.write_text(json.dumps(nb, indent=2))
    return f"Synced `{py_path.name}` into `{ipynb_path.relative_to(get_workspace_root())}` ({len(py_text.splitlines())} lines)."


@app.tool()
async def list_notebook_names(limit: int = 100, head: int = 15) -> str:
    """List notebook names and slugs from your Kaggle account."""
    proc = _run_kaggle(["kernels", "list", "--mine", "--page-size", str(max(1, limit)), "--sort-by", "dateRun", "--csv"], timeout=60)
    rows = _parse_kernel_csv(proc.stdout)
    if not rows:
        return "No notebooks found on Kaggle account."

    lines = [f"{len(rows)} notebooks on account:"]
    for row in rows[:head]:
        ref = row.get("ref", "")
        title = row.get("title", "") or ref
        lines.append(f"- {title} | {ref} | last_run={row.get('lastRunTime', 'unknown')}")
    if len(rows) > head:
        lines.append(f"... {len(rows) - head} more notebooks")
    return "\n".join(lines)


@app.tool()
async def list_active_runs(limit: int = 50, head: int = 15) -> str:
    """List active or currently running Kaggle notebooks."""
    proc = _run_kaggle(["kernels", "list", "--mine", "--page-size", str(max(1, limit)), "--sort-by", "dateRun", "--csv"], timeout=60)
    rows = _parse_kernel_csv(proc.stdout)
    if not rows:
        return "No notebooks found."

    active_statuses = {"RUNNING", "QUEUED", "PREPARING", "PENDING", "STARTING", "CANCEL_REQUESTED"}
    active: list[str] = []

    for row in rows:
        ref = row.get("ref", "")
        if not ref:
            continue
        try:
            st_proc = _run_kaggle(["kernels", "status", ref], timeout=20)
            status_text = st_proc.stdout.strip()
            match = re.search(r'has status "([^"]+)"', status_text)
            status_label = _clean_status_label(match.group(1) if match else "")
            if status_label in active_statuses:
                active.append(f"- {row.get('title', ref)} | {ref} | status={status_label}")
        except Exception:
            continue

    if not active:
        return "No active/running notebook jobs found."
    lines = [f"{len(active)} active notebook runs:"] + active[:head]
    if len(active) > head:
        lines.append(f"... {len(active) - head} more active")
    return "\n".join(lines)


@app.tool()
async def view_status(notebook: str, tail: int = 15, fetch_logs: bool = False) -> str:
    """Check Kaggle notebook status.

    Fast execution by default. Set fetch_logs=True to download full execution logs into runs/<version>/logs.txt.
    """
    name, path, _ = _source_path(notebook)
    existed, kernel = await _find_kernel(name, name)
    ref = _full_slug(kernel)
    if not ref:
        owner = _default_owner()
        ref = f"{owner}/{_slugify(name)}" if owner else None
    if not ref:
        raise RuntimeError(f"Could not resolve Kaggle kernel for '{name}'. Push with save_notebook_file first.")

    cache = _read_cache(name)

    # 1. Fetch status fast
    status_proc = _run_kaggle(["kernels", "status", ref], timeout=30)
    status_text = status_proc.stdout.strip()
    match = re.search(r'has status "([^"]+)"', status_text)
    status_label = _clean_status_label(match.group(1) if match else "UNKNOWN")

    version_match = re.search(r"version\s+(\d+)", status_text, re.I)
    version = int(version_match.group(1)) if version_match else cache.get("last_version_number")

    run_dir = get_kaggle_root() / name / "runs" / str(version or 0)
    run_dir.mkdir(parents=True, exist_ok=True)

    log_snippet = ""
    if fetch_logs:
        try:
            logs_proc = _run_kaggle(["kernels", "logs", ref], timeout=60)
            logs_text = logs_proc.stdout
            (run_dir / "logs.txt").write_text(logs_text)
            build_run_summary(run_dir)
            lines = logs_text.splitlines()
            log_snippet = "\n".join(lines[-tail:]) if lines else "[empty logs]"
        except Exception as err:
            log_snippet = f"[failed to fetch logs: {err}]"

    # Update metadata
    cache_update = {
        **cache,
        "last_status": status_label,
        "last_status_utc": datetime.now(timezone.utc).isoformat(),
    }
    _write_cache(name, cache_update)
    _write_local_metadata(path, cache_update)

    res = f"Notebook '{name}' ({ref}): {status_label} (version {version or 'unknown'})"
    if log_snippet:
        res += f"\n\nRecent logs:\n```text\n{log_snippet}\n```"
    return res


@app.tool()
async def fetch_output(notebook: str, run_number: int | None = None) -> str:
    """Download notebook output into ./kaggle/<notebook>/runs/<n>/output/ and build compressed summaries."""
    name, _, _ = _source_path(notebook)
    existed, kernel = await _find_kernel(name, name)
    ref = _full_slug(kernel)
    if not ref:
        owner = _default_owner()
        ref = f"{owner}/{_slugify(name)}" if owner else None
    if not ref:
        raise RuntimeError(f"Could not resolve Kaggle kernel for '{name}'.")

    # Resolve run directory
    runs_dir = get_kaggle_root() / name / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    if run_number is not None:
        target_dir = runs_dir / str(run_number)
    else:
        existing_nums = [int(p.name) for p in runs_dir.iterdir() if p.is_dir() and p.name.isdigit()]
        next_num = max(existing_nums, default=0) + 1
        target_dir = runs_dir / str(next_num)

    output_dir = target_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Download outputs via Kaggle CLI
    try:
        _run_kaggle(["kernels", "output", ref, "-p", str(output_dir), "--force"], timeout=300)
    except Exception as err:
        return f"Output download failed: {err}"

    # 2. Download execution logs
    try:
        logs_proc = _run_kaggle(["kernels", "logs", ref], timeout=120)
        (target_dir / "logs.txt").write_text(logs_proc.stdout)
    except Exception:
        pass

    # 3. High-speed in-memory compression (generates summary.txt, tree.txt, logs.compressed.txt)
    summary_text = build_run_summary(target_dir)

    # Calculate downloaded size
    files = list(output_dir.rglob("*"))
    file_count = sum(1 for f in files if f.is_file())
    total_size = sum(f.stat().st_size for f in files if f.is_file())

    return (
        f"Fetched run outputs for '{name}' into `{target_dir.relative_to(get_workspace_root())}`:\n"
        f"- Files downloaded: {file_count} ({human_size(total_size)})\n\n"
        f"{summary_text[:1000]}"
    )


@app.tool()
async def cancel_run(notebook: str) -> str:
    """Cancel the active/running session for a Kaggle notebook."""
    name, path, _ = _source_path(notebook)
    existed, kernel = await _find_kernel(name, name)
    ref = _full_slug(kernel)
    if not ref:
        owner = _default_owner()
        ref = f"{owner}/{_slugify(name)}" if owner else None
    if not ref:
        raise RuntimeError(f"Could not resolve Kaggle kernel for '{name}'.")

    owner, slug = _split_kernel_slug(ref)
    if not owner or not slug:
        raise RuntimeError(f"Invalid kernel ref: {ref}")

    # Check status first
    status_proc = _run_kaggle(["kernels", "status", ref], timeout=20)
    status_text = status_proc.stdout.strip()
    match = re.search(r'has status "([^"]+)"', status_text)
    status_label = _clean_status_label(match.group(1) if match else "")

    if status_label in {"COMPLETE", "ERROR", "CANCEL_ACKNOWLEDGED", "FAILED"}:
        return f"{name} is already {status_label}; no active run to cancel."

    # Cancel via Kaggle SDK
    try:
        from kaggle.api.kaggle_api_extended import KaggleApi
        from kagglesdk.kernels.types.kernels_api_service import ApiCancelKernelSessionRequest

        cache = _read_cache(name)
        session_id = cache.get("kernel_session_id") or cache.get("kernelSessionId")
        if not session_id:
            raise RuntimeError(f"No active session ID found in cache for {name} to cancel.")

        api = KaggleApi()
        api.authenticate()
        with api.build_kaggle_client() as client:
            req = ApiCancelKernelSessionRequest()
            req.kernel_session_id = int(session_id)
            client.kernels.kernels_api_client.cancel_kernel_session(req)
        return f"Cancel requested for {name} (session {session_id})."
    except Exception as err:
        raise RuntimeError(f"Failed to cancel session for {name}: {err}") from err


@app.tool()
async def pull_notebook(notebook: str, fetch_latest_output: bool = True) -> str:
    """Pull an existing Kaggle notebook and scaffold ./kaggle/<notebook>/ with runs and working directories."""
    proc = _run_kaggle(["kernels", "list", "--mine", "--page-size", "100", "--sort-by", "dateRun", "--csv"], timeout=60)
    rows = _parse_kernel_csv(proc.stdout)
    needle = notebook.strip().casefold()

    match: dict[str, str] | None = None
    for row in rows:
        ref = row.get("ref", "")
        _, slug = _split_kernel_slug(ref)
        title = (row.get("title") or "").strip()
        if needle in {ref.casefold(), slug.casefold(), title.casefold(), _sanitize_local_name(title).casefold()}:
            match = row
            break
    if not match:
        raise RuntimeError(f"Notebook '{notebook}' not found in your Kaggle account list.")

    ref = match.get("ref", "")
    title = (match.get("title") or ref.split("/")[-1]).strip()
    local_name = _sanitize_local_name(title)
    folder = get_kaggle_root() / local_name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "working").mkdir(parents=True, exist_ok=True)
    (folder / "runs").mkdir(parents=True, exist_ok=True)

    # Pull notebook code into a temporary probe directory
    probe_dir = folder / "working" / "pull_probe"
    probe_dir.mkdir(parents=True, exist_ok=True)
    _run_kaggle(["kernels", "pull", ref, "-p", str(probe_dir)], timeout=180)

    pulled = sorted(probe_dir.glob("*.ipynb")) + sorted(probe_dir.glob("*.py"))
    if not pulled:
        raise FileNotFoundError(f"No .ipynb or .py file returned from pull for {ref}")
    target_path = folder / ("notebook.ipynb" if pulled[0].suffix == ".ipynb" else pulled[0].name)
    shutil.copy(pulled[0], target_path)
    shutil.rmtree(probe_dir, ignore_errors=True)

    # Check status and version
    status_proc = _run_kaggle(["kernels", "status", ref], timeout=30)
    status_text = status_proc.stdout.strip()
    match_st = re.search(r'has status "([^"]+)"', status_text)
    status_label = _clean_status_label(match_st.group(1) if match_st else "UNKNOWN")

    version_match = re.search(r"version\s+(\d+)", status_text, re.I)
    version = int(version_match.group(1)) if version_match else 1

    run_dir = folder / "runs" / str(version)
    run_dir.mkdir(parents=True, exist_ok=True)
    output_msg = ""
    if fetch_latest_output:
        out_dir = run_dir / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            _run_kaggle(["kernels", "output", ref, "-p", str(out_dir), "--force"], timeout=180)
            logs_proc = _run_kaggle(["kernels", "logs", ref], timeout=60)
            (run_dir / "logs.txt").write_text(logs_proc.stdout)
            build_run_summary(run_dir)
            output_msg = f"\nLatest output and compressed summary saved to `{run_dir.relative_to(get_workspace_root())}`."
        except Exception:
            output_msg = "\n(Output download skipped or not yet available)."

    cache_update = {
        "title": title,
        "slug": ref,
        "id": ref,
        "source_path": str(target_path.relative_to(get_workspace_root())),
        "last_status": status_label,
        "last_version_number": version,
        "pulled_utc": datetime.now(timezone.utc).isoformat(),
    }
    _write_cache(local_name, cache_update)
    _write_local_metadata(target_path, cache_update)

    return f"Pulled '{ref}' into `{folder.relative_to(get_workspace_root())}` (version {version}, status {status_label}).{output_msg}"

# ---------------------------------------------------------------------------
# Dataset Tools
# ---------------------------------------------------------------------------

@app.tool()
async def upload_dataset(
    path: str,
    title: str | None = None,
    dataset_slug: str | None = None,
    private: bool = True,
    version_notes: str | None = None,
    keep_tabular: bool = True,
    dir_mode: str = "zip",
) -> str:
    """Upload or update a dataset on Kaggle.

    Can take a directory or a single file (CSV, parquet, zip, etc.).
    If the dataset already exists on your Kaggle account, pushes a new version.
    If it is new, creates the dataset on Kaggle.
    Local dataset folder is maintained under ./kaggle/datasets/<dataset_slug>/.
    """
    raw_path = Path(path)
    source_path = raw_path if raw_path.is_absolute() else get_workspace_root() / raw_path
    if not source_path.exists():
        raise FileNotFoundError(f"Dataset path not found: {source_path}")

    owner = _default_owner()
    if not owner:
        raise RuntimeError("Could not determine Kaggle username. Set KAGGLE_USERNAME or configure ~/.kaggle/kaggle.json.")

    slug_candidate = dataset_slug or (source_path.stem if source_path.is_file() else source_path.name)
    slug = _slugify(slug_candidate)
    dataset_title = title or slug.replace("-", " ").title()
    ref = f"{owner}/{slug}"

    datasets_dir = get_kaggle_root() / "datasets" / slug
    datasets_dir.mkdir(parents=True, exist_ok=True)

    # If source is a file, stage it into datasets_dir
    if source_path.is_file():
        dest_file = datasets_dir / source_path.name
        if source_path.resolve() != dest_file.resolve():
            shutil.copy(source_path, dest_file)
        upload_folder = datasets_dir
    else:
        upload_folder = source_path

    # Ensure dataset-metadata.json exists
    meta_path = upload_folder / "dataset-metadata.json"
    if not meta_path.exists():
        meta_content = {
            "title": dataset_title,
            "id": ref,
            "licenses": [{"name": "CC0-1.0"}],
        }
        meta_path.write_text(json.dumps(meta_content, indent=2))

    # Check if dataset exists on Kaggle
    existed = False
    try:
        status_proc = _run_kaggle(["datasets", "status", ref], timeout=20)
        if "ready" in status_proc.stdout.lower() or "pending" in status_proc.stdout.lower():
            existed = True
    except Exception:
        existed = False

    if existed:
        notes = version_notes or f"Updated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}"
        cmd = ["datasets", "version", "-p", str(upload_folder), "-m", notes, "-r", dir_mode]
        if keep_tabular:
            cmd.append("-t")
        proc = _run_kaggle(cmd, timeout=600)
        action_msg = f"Created new version of existing dataset '{ref}' ({notes})."
    else:
        cmd = ["datasets", "create", "-p", str(upload_folder), "-r", dir_mode]
        if not private:
            cmd.append("-u")
        if keep_tabular:
            cmd.append("-t")
        proc = _run_kaggle(cmd, timeout=600)
        action_msg = f"Created new dataset '{ref}' (private={private})."

    # Save local metadata.json
    local_meta = {
        "id": ref,
        "ref": ref,
        "slug": slug,
        "title": dataset_title,
        "owner": owner,
        "private": private,
        "last_updated_utc": datetime.now(timezone.utc).isoformat(),
        "source_folder": str(upload_folder.relative_to(get_workspace_root())),
    }
    (datasets_dir / "metadata.json").write_text(json.dumps(local_meta, indent=2))

    return f"{action_msg}\nURL: https://www.kaggle.com/datasets/{ref}"


@app.tool()
async def download_dataset(
    dataset: str,
    target_dir: str | None = None,
    unzip: bool = True,
) -> str:
    """Download a Kaggle dataset into the local workspace.

    Target directory defaults to ./kaggle/datasets/<dataset_name>/.
    """
    clean_ref = dataset.strip()
    slug = clean_ref.split("/")[-1]
    if target_dir:
        raw = Path(target_dir)
        dest = raw if raw.is_absolute() else get_workspace_root() / raw
    else:
        dest = get_kaggle_root() / "datasets" / slug
    dest.mkdir(parents=True, exist_ok=True)

    cmd = ["datasets", "download", clean_ref, "-p", str(dest)]
    if unzip:
        cmd.append("--unzip")

    proc = _run_kaggle(cmd, timeout=600)

    files = list(dest.rglob("*"))
    file_count = sum(1 for f in files if f.is_file())
    total_size = sum(f.stat().st_size for f in files if f.is_file())
    tree_text = compact_directory_tree(dest)

    return (
        f"Downloaded dataset '{clean_ref}' into `{dest.relative_to(get_workspace_root())}`:\n"
        f"- Files: {file_count} ({human_size(total_size)})\n\n"
        f"```text\n{tree_text}\n```"
    )


@app.tool()
async def list_datasets(
    search: str | None = None,
    mine: bool = True,
    page: int = 1,
    sort_by: str = "updated",
    head: int = 15,
) -> str:
    """List Kaggle datasets (your own datasets, or search public datasets)."""
    cmd = ["datasets", "list", "--csv"]
    if mine and not search:
        cmd.append("--mine")
    if search:
        cmd.extend(["--search", search])
    if page > 1:
        cmd.extend(["-p", str(page)])
    if sort_by in {"hottest", "votes", "updated", "active"}:
        cmd.extend(["--sort-by", sort_by])
    proc = _run_kaggle(cmd, timeout=60)
    rows = _parse_kernel_csv(proc.stdout)
    if not rows:
        return f"No datasets found{' matching ' + search if search else ''}."

    lines = [f"{len(rows)} datasets found:"]
    for row in rows[:head]:
        ref = row.get("ref", "")
        title = row.get("title", "") or ref
        size = row.get("size", "")
        try:
            size_str = human_size(int(size)) if size.isdigit() else size
        except Exception:
            size_str = size
        lines.append(f"- {title} | {ref} | size={size_str} | updated={row.get('lastUpdated', 'unknown')}")
    if len(rows) > head:
        lines.append(f"... {len(rows) - head} more datasets")
    return "\n".join(lines)


@app.tool()
async def get_dataset_status(dataset: str) -> str:
    """Check the creation/processing status of a Kaggle dataset."""
    proc = _run_kaggle(["datasets", "status", dataset.strip()], timeout=30)
    status_text = proc.stdout.strip() or "UNKNOWN"
    return f"Dataset '{dataset}' status: {status_text}"

@app.tool()
async def get_quota() -> str:
    """Show your weekly Kaggle GPU and TPU accelerator quota (used, remaining, total, refresh date)."""
    try:
        proc = _run_kaggle(["quota", "--csv"], timeout=30)
        rows = _parse_kernel_csv(proc.stdout)
        if not rows:
            return proc.stdout.strip() or "Quota information unavailable."
        lines = ["Weekly Accelerator Quota:"]
        for row in rows:
            res = row.get("resource", "Unknown")
            used = row.get("used", "0")
            remaining = row.get("remaining", "0")
            total = row.get("total", "0")
            refresh = row.get("refreshAt", "unknown")
            lines.append(f"- {res}: {remaining} remaining / {total} total ({used} used) — refreshes at {refresh}")
        return "\n".join(lines)
    except Exception as err:
        return f"Failed to retrieve quota: {err}"


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kaggle Local MCP Server")
    parser.add_argument("--config", type=Path, help="Path to machine-local kaggle.toml")
    parser.add_argument("--workspace-root", type=Path, help="Override workspace root directory")
    return parser.parse_args()


def load_config(config_path: Path | None) -> None:
    global WORKSPACE_ROOT, CONFIG_USERNAME, CONFIG_API_TOKEN
    if not config_path or not config_path.exists():
        return
    try:
        with config_path.open("rb") as f:
            data = tomllib.load(f)
        ws = data.get("workspace_root")
        if ws and str(ws).strip():
            WORKSPACE_ROOT = Path(str(ws).strip()).resolve()
        user = data.get("username")
        if user and str(user).strip():
            CONFIG_USERNAME = str(user).strip()
        token = data.get("api_token")
        if token and str(token).strip():
            CONFIG_API_TOKEN = str(token).strip()
    except Exception as err:
        sys.stderr.write(f"Warning: Failed to load config from {config_path}: {err}\n")


def main() -> None:
    global WORKSPACE_ROOT
    args = parse_args()
    if args.workspace_root:
        WORKSPACE_ROOT = args.workspace_root.resolve()
    elif os.environ.get("KAGGLE_WORKSPACE_ROOT"):
        WORKSPACE_ROOT = Path(os.environ["KAGGLE_WORKSPACE_ROOT"]).resolve()

    load_config(args.config)
    app.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
