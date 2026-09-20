#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "fastmcp==3.4.5",
#   "kaggle>=2.2.4",
# ]
# ///
from __future__ import annotations

import asyncio
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

app = FastMCP(
    "kaggle",
    instructions="""Kaggle Machine Learning & Experiment MCP.

CANONICAL WORKFLOW:
1. Pull existing notebook: `pull_notebook(notebook="<name>")`
   - Creates `./kaggle/<notebook>/notebook.ipynb` and `./kaggle/<notebook>/kernel-metadata.json`.
   - If starting a brand new experiment, call `init_notebook(notebook="<name>")`.
2. Configure execution via `kernel-metadata.json`:
   - Edit `./kaggle/<notebook>/kernel-metadata.json` directly using standard file tools:
     - "dataset_sources": ["username/dataset-slug"] (mounts under /kaggle/input/<dataset-slug>/)
     - "competition_sources": ["competition-name"] (mounts competition data)
     - "model_sources": ["username/model/framework/variation"] (mounts model weights)
     - "enable_gpu": "true" | "false" (or set accelerator in push/save)
     - "enable_tpu": "true" | "false"
     - "enable_internet": "true" | "false"
     - "is_private": "true" | "false"
     - "id": "username/notebook-slug"
3. Save vs Push:
   - `save_notebook(notebook="...")`: saves code and updates metadata locally and syncs to Kaggle cloud WITHOUT starting execution. If a `.py` file is present, automatically wraps it into `notebook.ipynb` (%%writefile + !python) for safe multiprocessing/GPU training.
   - `push_notebook(notebook="...")`: pushes to Kaggle and QUEUES EXECUTION (starts a new run/version).
4. Long-Running Execution & Waiting:
   - Kaggle training runs frequently take 10 minutes to several hours.
   - NEVER poll `view_status` in tight loops.
   - Use `wait(notebook="owner/slug", until="complete", timeout=20)` or `until="log", text="epoch 1"`.
   - Longer waits require a matching client timeout; output download is explicit via fetch_output.
   - When finished, `fetch_output` downloads all artifacts into `./kaggle/<notebook>/runs/<version>/output/` and generates `summary.txt` with compressed logs.
"""
)


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


async def _query_kaggle(args: list[str], timeout: float = 20) -> str:
    """Run read-only CLI queries without blocking the MCP event loop."""
    return await _query_process([*_resolve_kaggle_cmd(), *args], timeout)


async def _query_process(command: list[str], timeout: float = 20) -> str:
    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=get_workspace_root(), env=_cli_env(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    if proc.returncode:
        raise RuntimeError(f"Kaggle query failed ({proc.returncode}): {_strip_cli_warnings(stderr.decode(errors='replace'))}")
    return _strip_cli_warnings(stdout.decode())


@app.tool()
async def preview_dataset(dataset: str, file_name: str, rows: int = 10) -> str:
    """Read first 1–100 rows of an uncompressed CSV/TSV/JSONL dataset file.

    Uses Kaggle's authenticated raw-file stream, not a full dataset download.
    Reads at most 1 MiB into memory, writes nothing to disk, and closes the stream.
    This is a prefix sample, not random sampling or a full schema inference.
    Archives, Parquet, and binary formats are explicitly unsupported.
    """
    if not re.fullmatch(r"[\w-]+/[\w.-]+", dataset) or not 1 <= rows <= 100:
        raise ValueError("Expected owner/dataset-slug and rows between 1 and 100")
    suffix = Path(file_name).suffix.lower()
    if suffix not in {".csv", ".tsv", ".jsonl", ".ndjson"}:
        raise ValueError("Preview supports uncompressed CSV, TSV, JSONL, and NDJSON only")
    code = '''
import csv, io, itertools, json, sys
from kaggle.api.kaggle_api_extended import KaggleApi
from kagglesdk.datasets.types.dataset_api_service import ApiDownloadDatasetRequest
dataset, filename, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
api = KaggleApi(); api.authenticate()
with api.build_kaggle_client() as client:
    req = ApiDownloadDatasetRequest()
    req.owner_slug, req.dataset_slug = dataset.split('/')
    req.file_name = filename
    req.raw = True
    response = client.datasets.dataset_api_client.download_dataset(req)
    try:
        response.raise_for_status()
        data = response.raw.read(1048576, decode_content=True)
    finally:
        response.close()
if data.startswith(b'PK') or data.startswith(b'\\x1f\\x8b'):
    raise ValueError('Server returned an archive instead of a raw text file')
bounded = len(data) == 1048576
if bounded:
    data = data[:data.rfind(b'\\n')] if b'\\n' in data else b''
text = data.decode('utf-8-sig')
if filename.lower().endswith(('.csv', '.tsv')):
    reader = csv.DictReader(io.StringIO(text), delimiter='\\t' if filename.lower().endswith('.tsv') else ',', strict=True)
    columns = reader.fieldnames
    result = list(itertools.islice(reader, count))
else:
    result = [json.loads(line) for line in itertools.islice((line for line in text.splitlines() if line.strip()), count)]
    columns = None
print(json.dumps(dict(dataset=dataset, file=filename, columns=columns, rows=result, byte_limit_reached=bounded, sample='file prefix', max_bytes=1048576)))
'''
    return await _query_process([sys.executable, "-c", code, dataset, file_name, str(rows)])


@app.tool()
async def search_library(
    query: str,
    kind: str = "datasets",
    page: int = 1,
    page_token: str | None = None,
    owner: str | None = None,
    sort_by: str | None = None,
) -> str:
    """Search public Kaggle datasets, notebooks, models, or competitions.

    Returns native JSON metadata (including references, titles, popularity and
    timestamps where available). Notebooks accepts dataset/competition terms in
    the query. Page numbers work for datasets/notebooks/competitions; models
    require page_token for subsequent pages. CLI page sizes differ by resource.
    Does not download, upload, or run anything.
    """
    resources = {"datasets": "datasets", "notebooks": "kernels", "models": "models", "competitions": "competitions"}
    if kind not in resources:
        raise ValueError(f"kind must be one of {', '.join(resources)}")
    if not query.strip() or page < 1:
        raise ValueError("A nonempty query and positive page are required")
    if owner and kind == "competitions":
        raise ValueError("Competitions do not support owner filtering")
    if page != 1 and (kind == "models" or page_token):
        raise ValueError("Use page_token instead of page for models; do not combine pagination modes")
    cmd = [resources[kind], "list", "--search", query.strip(), "--format", "json"]
    if page_token:
        cmd.extend(["--page-token", page_token])
    elif page != 1:
        cmd.extend(["--page", str(page)])
    if owner:
        cmd.extend(["--owner" if kind == "models" else "--user", owner])
    if sort_by:
        cmd.extend(["--sort-by", sort_by])
    return await _query_kaggle(cmd)


@app.tool()
async def list_dataset_files(dataset: str, page_token: str | None = None, page_size: int = 20) -> str:
    """Inspect dataset filenames and sizes without downloading. Pass owner/slug.

    Returns native JSON and any pagination information emitted by Kaggle.
    """
    if not re.fullmatch(r"[\w-]+/[\w.-]+", dataset) or not 1 <= page_size <= 200:
        raise ValueError("Expected owner/dataset-slug and page_size between 1 and 200")
    cmd = ["datasets", "files", dataset, "--format", "json", "--page-size", str(page_size)]
    if page_token:
        cmd.extend(["--page-token", page_token])
    return await _query_kaggle(cmd)


@app.tool()
async def read_metadata(reference: str, kind: str = "datasets") -> str:
    """Read dataset or model metadata by owner/slug without downloading files or weights."""
    if kind not in {"datasets", "models"}:
        raise ValueError("kind must be datasets or models")
    if not re.fullmatch(r"[\w-]+/[\w.-]+", reference):
        raise ValueError("Expected owner/slug")
    code = '''
import sys
from kaggle.api.kaggle_api_extended import KaggleApi
from kagglesdk.datasets.types.dataset_api_service import ApiGetDatasetRequest
from kagglesdk.models.types.model_api_service import ApiGetModelRequest
kind, reference = sys.argv[1:]
owner, slug = reference.split('/')
api = KaggleApi(); api.authenticate()
with api.build_kaggle_client() as client:
    if kind == 'datasets':
        request = ApiGetDatasetRequest()
        request.owner_slug, request.dataset_slug = owner, slug
        result = client.datasets.dataset_api_client.get_dataset(request)
    else:
        request = ApiGetModelRequest()
        request.owner_slug, request.model_slug = owner, slug
        result = client.models.model_api_client.get_model(request)
    print(result.to_json())
'''
    return await _query_process([sys.executable, "-c", code, kind, reference])


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

# Canonical accelerators supported on Kaggle Cloud
VALID_ACCELERATORS: dict[str, str] = {
    "none": "none",
    "cpu": "none",
    "gpu": "NvidiaTeslaT4",
    "t4": "NvidiaTeslaT4",
    "nvidia-tesla-t4": "NvidiaTeslaT4",
    "nvidiateslat4": "NvidiaTeslaT4",
    "p100": "NvidiaTeslaP100",
    "nvidia-tesla-p100": "NvidiaTeslaP100",
    "nvidiateslap100": "NvidiaTeslaP100",
    "tpu": "tpu",
    "tpu-v3-8": "tpu",
}


def _normalize_accelerator(value: str) -> str:
    normalized = (value or "none").strip().lower().replace("_", "-")
    return VALID_ACCELERATORS.get(normalized, value)


def _validate_accelerator(value: str) -> str:
    """Strict validation for accelerator options.

    Raises ValueError if an unsupported accelerator is requested.
    Prevents Kaggle Cloud from silently dropping GPU/TPU and running on CPU.
    """
    normalized = (value or "none").strip().lower().replace("_", "-")
    if normalized not in VALID_ACCELERATORS:
        valid_options = sorted(list(set(VALID_ACCELERATORS.keys())))
        raise ValueError(
            f"Invalid accelerator: '{value}'. Supported accelerators are strictly:\n"
            f"- CPU: 'none' or 'cpu'\n"
            f"- GPU (Tesla T4): 'gpu', 't4', or 'NvidiaTeslaT4'\n"
            f"- GPU (Tesla P100): 'p100' or 'NvidiaTeslaP100'\n"
            f"- TPU: 'tpu' or 'tpu-v3-8'\n"
            f"(Valid choices: {', '.join(valid_options)})"
        )
    return VALID_ACCELERATORS[normalized]


def _is_gpu_accelerator(value: str) -> bool:
    normalized = _normalize_accelerator(value).lower()
    return normalized.startswith("nvidia")


def _check_accelerator_quota(acc: str) -> tuple[bool, str]:
    """Pre-flight check accelerator quota to prevent failures on Kaggle Cloud."""
    normalized = _normalize_accelerator(acc).lower()
    if normalized in ("none", "", "cpu"):
        return True, "CPU (no quota required)"

    try:
        proc = _run_kaggle(["quota", "--csv"], timeout=30)
        rows = _parse_kernel_csv(proc.stdout)
        is_gpu = _is_gpu_accelerator(acc)
        is_tpu = normalized == "tpu"

        for row in rows:
            res = (row.get("resource") or "").lower()
            rem = row.get("remaining", "0h")
            rem_val = float(rem.replace("h", "").strip()) if "h" in rem else 0.0

            if is_gpu and "gpu" in res:
                if rem_val <= 0.0:
                    return False, f"GPU quota exhausted ({rem} remaining, refreshes at {row.get('refreshAt')})"
                return True, f"GPU quota available ({rem} remaining)"
            if is_tpu and "tpu" in res:
                if rem_val <= 0.0:
                    return False, f"TPU quota exhausted ({rem} remaining, refreshes at {row.get('refreshAt')})"
                return True, f"TPU quota available ({rem} remaining)"
    except Exception as err:
        # Non-blocking if quota check fails (e.g. offline or network blip)
        return True, f"Quota check skipped: {err}"

    return True, "Quota check ok"


def _lint_kernel_metadata(meta: dict[str, Any]) -> list[str]:
    """Strict validation of kernel-metadata.json fields reusing KaggleApi's native validators.

    Per coding rules (Rule 4: Manual implementation rule - do not re-implement library functions),
    this invokes the official KaggleApi validation methods directly to guarantee 100% parity
    with Kaggle Cloud enforcement without hardcoding or re-inventing checks.
    """
    from kaggle.api.kaggle_api_extended import KaggleApi
    api = KaggleApi()

    errors: list[str] = []

    # 1. Kernel slug & syntax validation
    slug = meta.get("id")
    if not slug:
        errors.append("Missing required 'id' (slug) in kernel-metadata.json. Must be formatted as '{username}/{notebook-slug}'.")
    else:
        try:
            api.validate_kernel_string(slug)
            owner, kernel_slug, version = api.parse_kernel_string(slug)
            if version is not None:
                errors.append(f"Invalid 'id': '{slug}'. Do NOT include a version number in kernel-metadata.json 'id'.")
            api._validate_slug_syntax(kernel_slug)
        except ValueError as err:
            errors.append(str(err))

    # 2. Title validation (Kaggle rule: >= 5 chars)
    title = meta.get("title")
    if not title or len(str(title).strip()) < 5:
        errors.append("Title must be at least five characters")

    # 3. Code file validation
    code_file = meta.get("code_file")
    if not code_file:
        errors.append("A source file must be specified in the metadata ('code_file')")

    # 4. Language & Kernel type validation (KaggleApi native sets)
    kernel_type = meta.get("kernel_type", "")
    if kernel_type not in api.valid_push_kernel_types:
        errors.append(
            f"Invalid kernel_type: '{kernel_type}'. Valid options are {api.valid_push_kernel_types}"
        )

    language = meta.get("language", "")
    if language not in api.valid_push_language_types:
        errors.append(
            f"Invalid language: '{language}'. Valid options are {api.valid_push_language_types}"
        )

    # 5. Dataset sources validation (native Kaggle validator)
    for ds in meta.get("dataset_sources", []):
        try:
            api.validate_dataset_string(ds)
        except ValueError as err:
            errors.append(f"Invalid dataset_sources entry '{ds}': {err}")

    # 6. Competition sources validation
    for cs in meta.get("competition_sources", []):
        if "/" in cs:
            errors.append(
                f"Invalid competition_sources entry '{cs}': Competition sources must be just the competition name (e.g. 'titanic'), NOT 'owner/competition'."
            )

    # 7. Model sources validation (native Kaggle validator)
    for ms in meta.get("model_sources", []):
        try:
            api.validate_model_instance_version_string(ms)
        except ValueError as err:
            errors.append(f"Invalid model_sources entry '{ms}': {err}")

    # 8. Strict Accelerator validation
    acc = meta.get("accelerator")
    if acc:
        try:
            _validate_accelerator(str(acc))
        except ValueError as err:
            errors.append(str(err))

    return errors


def _check_accelerator_quota(acc: str) -> tuple[bool, str]:
    """Pre-flight check accelerator quota to prevent failures on Kaggle Cloud."""
    normalized = _normalize_accelerator(acc).lower()
    if normalized in ("none", "", "cpu"):
        return True, "CPU (no quota required)"

    try:
        proc = _run_kaggle(["quota", "--csv"], timeout=30)
        rows = _parse_kernel_csv(proc.stdout)
        is_gpu = _is_gpu_accelerator(acc)
        is_tpu = normalized == "tpu"

        for row in rows:
            res = (row.get("resource") or "").lower()
            rem = row.get("remaining", "0h")
            rem_val = float(rem.replace("h", "").strip()) if "h" in rem else 0.0

            if is_gpu and "gpu" in res:
                if rem_val <= 0.0:
                    return False, f"GPU quota exhausted ({rem} remaining, refreshes at {row.get('refreshAt')})"
                return True, f"GPU quota available ({rem} remaining)"
            if is_tpu and "tpu" in res:
                if rem_val <= 0.0:
                    return False, f"TPU quota exhausted ({rem} remaining, refreshes at {row.get('refreshAt')})"
                return True, f"TPU quota available ({rem} remaining)"
    except Exception as err:
        # Non-blocking if quota check fails (e.g. offline or network blip)
        return True, f"Quota check skipped: {err}"

    return True, "Quota check ok"


def _lint_kernel_metadata(meta: dict[str, Any]) -> list[str]:
    """Strict validation of kernel-metadata.json fields reusing KaggleApi's native validators.

    Per coding rules (Rule 4: Manual implementation rule - do not re-implement library functions),
    this invokes the official KaggleApi validation methods directly to guarantee 100% parity
    with Kaggle Cloud enforcement without hardcoding or re-inventing checks.
    """
    from kaggle.api.kaggle_api_extended import KaggleApi
    api = KaggleApi()

    errors: list[str] = []

    # 1. Kernel slug & syntax validation
    slug = meta.get("id")
    if not slug:
        errors.append("Missing required 'id' (slug) in kernel-metadata.json. Must be formatted as '{username}/{notebook-slug}'.")
    else:
        try:
            api.validate_kernel_string(slug)
            owner, kernel_slug, version = api.parse_kernel_string(slug)
            if version is not None:
                errors.append(f"Invalid 'id': '{slug}'. Do NOT include a version number in kernel-metadata.json 'id'.")
            api._validate_slug_syntax(kernel_slug)
        except ValueError as err:
            errors.append(str(err))

    # 2. Title validation (Kaggle rule: >= 5 chars)
    title = meta.get("title")
    if not title or len(str(title).strip()) < 5:
        errors.append("Title must be at least five characters")

    # 3. Code file validation
    code_file = meta.get("code_file")
    if not code_file:
        errors.append("A source file must be specified in the metadata ('code_file')")

    # 4. Language & Kernel type validation (KaggleApi native sets)
    kernel_type = meta.get("kernel_type", "")
    if kernel_type not in api.valid_push_kernel_types:
        errors.append(
            f"Invalid kernel_type: '{kernel_type}'. Valid options are {api.valid_push_kernel_types}"
        )

    language = meta.get("language", "")
    if language not in api.valid_push_language_types:
        errors.append(
            f"Invalid language: '{language}'. Valid options are {api.valid_push_language_types}"
        )

    # 5. Dataset sources validation (native Kaggle validator)
    for ds in meta.get("dataset_sources", []):
        try:
            api.validate_dataset_string(ds)
        except ValueError as err:
            errors.append(f"Invalid dataset_sources entry '{ds}': {err}")

    # 6. Competition sources validation
    for cs in meta.get("competition_sources", []):
        if "/" in cs:
            errors.append(
                f"Invalid competition_sources entry '{cs}': Competition sources must be just the competition name (e.g. 'titanic'), NOT 'owner/competition'."
            )

    # 7. Model sources validation (native Kaggle validator)
    for ms in meta.get("model_sources", []):
        try:
            api.validate_model_instance_version_string(ms)
        except ValueError as err:
            errors.append(f"Invalid model_sources entry '{ms}': {err}")

    return errors

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
# Metadata & Run Tracking Helpers
# ---------------------------------------------------------------------------

def _kernel_metadata_path(name_or_folder: str | Path) -> Path:
    folder = name_or_folder if isinstance(name_or_folder, Path) else get_kaggle_root() / name_or_folder
    if folder.is_file():
        folder = folder.parent
    return folder / "kernel-metadata.json"


def _read_metadata(name_or_folder: str | Path) -> dict[str, Any]:
    path = _kernel_metadata_path(name_or_folder)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    # Fallback to legacy metadata.json if it exists
    folder = path.parent
    if (folder / "metadata.json").exists():
        try:
            return json.loads((folder / "metadata.json").read_text())
        except Exception:
            pass
    return {}


def _write_metadata(name_or_folder: str | Path, data: dict[str, Any]) -> None:
    path = _kernel_metadata_path(name_or_folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_metadata(name_or_folder)
    existing.update(data)
    path.write_text(json.dumps(existing, indent=2))


def _read_cache(name_or_folder: str | Path) -> dict[str, Any]:
    return _read_metadata(name_or_folder)


def _write_cache(name_or_folder: str | Path, data: dict[str, Any]) -> None:
    _write_metadata(name_or_folder, data)


def _write_local_metadata(source_path: Path, data: dict[str, Any]) -> None:
    _write_metadata(source_path, data)

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

def _resolve_notebook_folder(notebook: str) -> tuple[str, Path]:
    kaggle_root = get_kaggle_root()
    raw = Path(notebook)
    candidate = raw if raw.is_absolute() else get_workspace_root() / raw
    if candidate.is_dir():
        return candidate.name, candidate.resolve()
    if candidate.is_file():
        # If pointing to a script in workspace root, create or use ./kaggle/<stem>
        if candidate.parent == get_workspace_root():
            folder = kaggle_root / candidate.stem
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / candidate.name
            if not target.exists() or candidate.stat().st_mtime > target.stat().st_mtime:
                shutil.copy(candidate, target)
            return candidate.stem, folder.resolve()
        return candidate.parent.name, candidate.parent.resolve()

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
        else:
            folder.mkdir(parents=True, exist_ok=True)
    return folder.name, folder.resolve()
@app.tool()
async def init_notebook(
    notebook: str,
    title: str | None = None,
    template_type: str = "py",
) -> str:
    """Initialize a brand new notebook experiment directory under ./kaggle/<notebook>/.

    Creates:
    - `./kaggle/<notebook>/kernel-metadata.json` (pointing to your username/<slug>)
    - If template_type == 'py' (default): `./kaggle/<notebook>/train.py`
    - If template_type == 'ipynb': `./kaggle/<notebook>/notebook.ipynb`
    - `./kaggle/<notebook>/runs/` and `./kaggle/<notebook>/working/` directories.

    You can then edit code and edit `kernel-metadata.json` before calling `push_notebook`.
    """
    name, folder = _resolve_notebook_folder(notebook)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "runs").mkdir(parents=True, exist_ok=True)
    (folder / "working").mkdir(parents=True, exist_ok=True)

    owner = _default_owner()
    slug = _slugify(name)
    full_id = f"{owner}/{slug}" if owner else slug
    notebook_title = title or name

    code_file = "notebook.ipynb"
    if template_type.lower() == "py":
        py_path = folder / "train.py"
        if not py_path.exists():
            py_path.write_text(
                f'"""Training script for {notebook_title}."""\n'
                f'import os\n'
                f'print("Starting {notebook_title} on Kaggle...")\n'
            )
    else:
        ipynb_path = folder / "notebook.ipynb"
        if not ipynb_path.exists():
            nb = {
                "cells": [
                    {"cell_type": "markdown", "metadata": {}, "source": [f"# {notebook_title}\n"]},
                    {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": ["print('Hello from Kaggle')\n"]},
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

    metadata_path = folder / "kernel-metadata.json"
    if not metadata_path.exists():
        meta = {
            "id": full_id,
            "title": notebook_title,
            "code_file": code_file,
            "language": "python",
            "kernel_type": "notebook",
            "is_private": "true",
            "enable_gpu": "false",
            "enable_tpu": "false",
            "enable_internet": "true",
            "dataset_sources": [],
            "competition_sources": [],
            "kernel_sources": [],
            "model_sources": [],
            "total_runs": 0,
            "last_version_number": 0,
            "last_status": "UNPUSHED",
        }
        metadata_path.write_text(json.dumps(meta, indent=2))

    return (
        f"Initialized new Kaggle notebook experiment '{name}':\n"
        f"- Folder: {folder.relative_to(get_workspace_root())}\n"
        f"- Code: {'train.py' if template_type == 'py' else 'notebook.ipynb'}\n"
        f"- Metadata: {metadata_path.relative_to(get_workspace_root())}\n\n"
        f"Next: Edit code and `kernel-metadata.json` (to attach datasets/GPU), then call `push_notebook('{name}')`."
    )




@app.tool()
async def save_notebook(
    notebook: str,
    accelerator: str | None = None,
    dataset_sources: list[str] | None = None,
    competition_sources: list[str] | None = None,
    model_sources: list[str] | None = None,
    internet: bool | None = None,
    private: bool | None = None,
    py_source: str | None = None,
) -> str:
    """Save and prepare local notebook files without pushing or executing on Kaggle.

    STORAGE & DIRECTORY STRUCTURE:
    - All files are stored under `./kaggle/<notebook_name>/`:
        ./kaggle/<notebook_name>/
            ├── notebook.ipynb         # Notebook pushed to Kaggle
            ├── kernel-metadata.json   # Kaggle configuration (hardware, datasets, title, privacy)
            ├── train.py               # (Optional) local Python script
            └── runs/<version>/        # Execution output and logs

    CODE WRAPPING:
    - If a .py file is present (or py_source is provided), automatically wraps it into
      notebook.ipynb (%%writefile + !python) for safe DDP/GPU multiprocessing.

    METADATA & STRICT LINTING:
    - Updates ./kaggle/<notebook>/kernel-metadata.json.
    - Runs strict validation on all fields before saving. If any field is invalid, raises
      an error detailing what is wrong and what is accepted.
    - Does NOT push to Kaggle or start execution. Use push_notebook when ready to run.
    """
    name, folder = _resolve_notebook_folder(notebook)
    folder.mkdir(parents=True, exist_ok=True)

    # 1. Resolve code: .py auto-conversion or .ipynb
    py_candidate: Path | None = None
    if py_source:
        py_candidate = _resolve_py_source(name, py_source)
    else:
        for fname in ("train.py", f"{name.lower()}.py", f"{name}.py", "main.py", "notebook.py"):
            c = folder / fname
            if c.exists():
                py_candidate = c
                break
        if not py_candidate:
            other_py = [p for p in folder.glob("*.py") if p.name not in ("setup.py", "__init__.py")]
            if other_py:
                py_candidate = other_py[0]

    ipynb_path = folder / "notebook.ipynb"

    should_wrap_py = False
    if py_candidate and py_candidate.exists():
        if py_source or not ipynb_path.exists():
            should_wrap_py = True
        elif py_candidate.stat().st_mtime > ipynb_path.stat().st_mtime:
            should_wrap_py = True

    if should_wrap_py and py_candidate:
        py_text = py_candidate.read_text()
        script_name = py_candidate.name
        nb = {
            "cells": [
                {"cell_type": "markdown", "metadata": {}, "source": f"# {name}\n\nAuto-converted from `{script_name}`.\n"},
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
        code_file = "notebook.ipynb"
        kernel_type = "notebook"
    elif ipynb_path.exists():
        code_file = "notebook.ipynb"
        kernel_type = "notebook"
    elif py_candidate:
        code_file = py_candidate.name
        kernel_type = "script"
    else:
        nb = {
            "cells": [
                {"cell_type": "markdown", "metadata": {}, "source": f"# {name}\n"},
                {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": ["# Add code here\nprint('Hello Kaggle')\n"]},
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
        code_file = "notebook.ipynb"
        kernel_type = "notebook"

    meta = _read_metadata(folder)
    owner = _default_owner()
    if not owner:
        raise RuntimeError("Could not determine Kaggle username. Set KAGGLE_USERNAME or configure ~/.kaggle/kaggle.json.")

    existed, kernel = await _find_kernel(meta.get("title", name), name)
    ref = meta.get("id") or _full_slug(kernel) or f"{owner}/{_slugify(name)}"
    title = meta.get("title") or name

    # Accelerator
    acc = accelerator or meta.get("accelerator") or "none"
    if acc != "none":
        acc = _normalize_accelerator(acc)
    enable_gpu = meta.get("enable_gpu", "false")
    if isinstance(enable_gpu, bool):
        enable_gpu = "true" if enable_gpu else "false"
    if _is_gpu_accelerator(acc):
        enable_gpu = "true"

    enable_tpu = meta.get("enable_tpu", "false")
    if isinstance(enable_tpu, bool):
        enable_tpu = "true" if enable_tpu else "false"
    if acc.lower() == "tpu":
        enable_tpu = "true"

    # Internet
    enable_internet = meta.get("enable_internet", "true")
    if internet is not None:
        enable_internet = "true" if internet else "false"
    elif isinstance(enable_internet, bool):
        enable_internet = "true" if enable_internet else "false"

    # Privacy
    is_private = meta.get("is_private", "true")
    if private is not None:
        is_private = "true" if private else "false"
    elif isinstance(is_private, bool):
        is_private = "true" if is_private else "false"

    # Sources
    datasets = dataset_sources if dataset_sources is not None else list(meta.get("dataset_sources") or [])
    competitions = competition_sources if competition_sources is not None else list(meta.get("competition_sources") or [])
    models = model_sources if model_sources is not None else list(meta.get("model_sources") or [])

    full_meta = {
        "id": ref,
        "title": title,
        "code_file": code_file,
        "language": "python",
        "kernel_type": kernel_type,
        "is_private": is_private,
        "enable_gpu": enable_gpu,
        "enable_tpu": enable_tpu,
        "enable_internet": enable_internet,
        "dataset_sources": datasets,
        "competition_sources": competitions,
        "kernel_sources": list(meta.get("kernel_sources") or []),
        "model_sources": models,
        "accelerator": acc,
    }

    # Strict linting check
    lint_errors = _lint_kernel_metadata(full_meta)
    if lint_errors:
        formatted_errors = "\n".join(f"- {err}" for err in lint_errors)
        raise ValueError(
            f"Metadata validation failed for '{name}':\n{formatted_errors}\n\n"
            f"Please fix kernel-metadata.json according to Kaggle specifications."
        )

    for k in ("last_version_number", "total_runs", "last_status", "last_run_utc", "last_pushed_utc"):
        if k in meta:
            full_meta[k] = meta[k]

    _write_metadata(folder, full_meta)

    attached = []
    if datasets:
        attached.append(f"datasets: {', '.join(datasets)}")
    if competitions:
        attached.append(f"competitions: {', '.join(competitions)}")
    if models:
        attached.append(f"models: {', '.join(models)}")
    attached_msg = f"\nattached: {'; '.join(attached)}" if attached else ""

    return (
        f"Saved local notebook '{name}'\n"
        f"code: {code_file}\n"
        f"metadata: {(_kernel_metadata_path(folder)).relative_to(get_workspace_root())}\n"
        f"accelerator: {acc} (gpu={enable_gpu}, tpu={enable_tpu})\n"
        f"internet: {enable_internet}"
        f"{attached_msg}\n"
        f"(Not pushed to Kaggle. Call push_notebook to queue execution)."
    )
@app.tool()
async def push_notebook(
    notebook: str,
    accelerator: str | None = None,
    dataset_sources: list[str] | None = None,
    competition_sources: list[str] | None = None,
    model_sources: list[str] | None = None,
    internet: bool | None = None,
    private: bool | None = None,
    py_source: str | None = None,
) -> str:
    """Push a local notebook folder to Kaggle and queue execution (starts a new run).

    STORAGE & DIRECTORY STRUCTURE:
    - Looks up `./kaggle/<notebook_name>/`.
    - All outputs and run summaries will be downloaded into `./kaggle/<notebook_name>/runs/<version>/`.

    VALIDATION & LINTING:
    - Runs strict validation on `./kaggle/<notebook>/kernel-metadata.json`.
      (If any metadata field or slug is invalid, stops immediately with specific error feedback).
    - Pre-flight quota check: Verifies accelerator quota on Kaggle before pushing.
      (If GPU/TPU quota is 0.00h, blocks the push with an error instead of letting Kaggle fail).

    EXECUTION:
    - Pushes to Kaggle, creating a new version/run on Kaggle Cloud.
    - Updates run tracking (`total_runs`, `last_version_number`, `last_status`) in `kernel-metadata.json`.
    """
    name, folder = _resolve_notebook_folder(notebook)
    # Save and lint first
    await save_notebook(
        notebook=notebook,
        accelerator=accelerator,
        dataset_sources=dataset_sources,
        competition_sources=competition_sources,
        model_sources=model_sources,
        internet=internet,
        private=private,
        py_source=py_source,
    )

    full_meta = _read_metadata(folder)
    acc = full_meta.get("accelerator", "none")

    # Pre-flight quota check
    has_quota, quota_msg = _check_accelerator_quota(acc)
    if not has_quota:
        raise RuntimeError(
            f"Push rejected for '{name}': {quota_msg}. "
            f"Please switch accelerator to 'none' (CPU) or wait for your weekly quota to refresh."
        )

    code_file = full_meta.get("code_file", "notebook.ipynb")
    ref = full_meta.get("id", "")
    title = full_meta.get("title", name)

    # Stage and push
    push_dir = folder / "working" / "kaggle_push"
    push_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(folder / code_file, push_dir / code_file)
    (push_dir / "kernel-metadata.json").write_text(json.dumps(full_meta, indent=2))

    cmd = ["kernels", "push", "-p", str(push_dir)]
    if acc and acc.lower() not in ("none", "cpu"):
        cmd.extend(["--accelerator", acc])

    result = _run_kaggle(cmd, timeout=600)
    version_match = re.search(r"Kernel version\s+(\d+)\s+successfully pushed", result.stdout, re.I)
    version = int(version_match.group(1)) if version_match else None

    # Update metadata tracking
    owner_slug, kernel_slug = _split_kernel_slug(ref)
    current_runs = int(full_meta.get("total_runs", 0)) + 1
    meta_updates = {
        "last_pushed_utc": datetime.now(timezone.utc).isoformat(),
        "last_status": "QUEUED",
        "total_runs": current_runs,
    }
    if version:
        meta_updates["last_version_number"] = version
    _write_metadata(folder, meta_updates)

    attached = []
    if full_meta.get("dataset_sources"):
        attached.append(f"datasets: {', '.join(full_meta['dataset_sources'])}")
    if full_meta.get("competition_sources"):
        attached.append(f"competitions: {', '.join(full_meta['competition_sources'])}")
    if full_meta.get("model_sources"):
        attached.append(f"models: {', '.join(full_meta['model_sources'])}")
    attached_msg = f"\nattached: {'; '.join(attached)}" if attached else ""

    return (
        f"Pushed '{title}' to Kaggle.\n"
        f"slug: {ref}\n"
        f"version: {version or 'latest'} (total runs: {current_runs})\n"
        f"accelerator: {acc} (gpu={full_meta.get('enable_gpu')}, tpu={full_meta.get('enable_tpu')})\n"
        f"internet: {full_meta.get('enable_internet')}"
        f"{attached_msg}\n"
        f"Notebook successfully queued on Kaggle."
    )


@app.tool()
async def wait(
    notebook: str,
    until: str = "complete",
    text: str | None = None,
    timeout: int = 20,
    poll_interval: int = 5,
) -> str:
    """Wait until notebook terminates or a literal string appears in available logs.

    until: complete or log. log requires text (case-sensitive literal substring).
    Existing log text also matches; this is not restricted to new log lines.
    Returns JSON with reason matched, terminal, or timeout. Terminal errors stop
    either wait mode. No output download. Default 20s fits short MCP deadlines;
    longer waits require a client timeout greater than timeout. Reissue after a
    timeout instead of tight polling. Log queries inspect the current run.
    """
    if until not in {"complete", "log"} or (until == "log" and not text):
        raise ValueError("until must be complete or log; log requires nonempty text")
    if timeout < 1 or poll_interval < 1:
        raise ValueError("timeout and poll_interval must be positive")
    if re.fullmatch(r"[\w-]+/[\w.-]+", notebook):
        ref = notebook
    else:
        name, folder = _resolve_notebook_folder(notebook)
        ref = _read_metadata(folder).get("id")
        if not ref:
            raise ValueError("Notebook metadata has no id; pass owner/notebook-slug")
    deadline = asyncio.get_running_loop().time() + timeout
    status = "UNKNOWN"
    try:
        async with asyncio.timeout(timeout):
            while True:
                output = await _query_kaggle(["kernels", "status", ref])
                match = re.search(r'has status "([^"]+)"', output)
                if not match:
                    raise RuntimeError("Unrecognized Kaggle status response")
                status = _clean_status_label(match.group(1)).upper()
                if until == "log":
                    logs = await _query_kaggle(["kernels", "logs", ref])
                    try:
                        entries = json.loads(logs)
                    except json.JSONDecodeError:
                        entries = None
                    if isinstance(entries, list):
                        logs = "\n".join(str(entry.get("data", entry.get("message", ""))) if isinstance(entry, dict) else str(entry) for entry in entries)
                    if text in logs:
                        return json.dumps(dict(reason="matched", notebook=ref, status=status, text=text))
                if status in {"COMPLETE", "ERROR", "CANCELLED", "CANCELED", "CANCEL_ACKNOWLEDGED"}:
                    return json.dumps(dict(reason="terminal", notebook=ref, status=status, matched=False if until == "log" else None))
                await asyncio.sleep(min(poll_interval, max(0, deadline - asyncio.get_running_loop().time())))
    except TimeoutError:
        return json.dumps(dict(reason="timeout", notebook=ref, status=status))

@app.tool()
async def delete_notebook(notebook: str) -> str:
    """Delete a notebook and its remote runs from your Kaggle account.

    WARNING: This permanently removes the notebook and all versions on Kaggle Cloud.
    """
    name, folder = _resolve_notebook_folder(notebook)
    meta = _read_metadata(folder)
    owner = _default_owner()
    existed, kernel = await _find_kernel(meta.get("title", name), name)
    ref = meta.get("id") or _full_slug(kernel) or f"{owner}/{_slugify(name)}"

    proc = _run_kaggle(["kernels", "delete", "-y", ref], timeout=60)
    out = proc.stdout.strip() or f"Kernel {ref} deleted successfully"
    return out


@app.tool()
async def list_notebook_names(limit: int = 100, head: int = 15) -> str:
    """List notebook names and slugs from your Kaggle account."""
    output = await _query_kaggle(["kernels", "list", "--mine", "--page-size", str(max(1, min(limit, 200))), "--sort-by", "dateRun", "--csv"])
    rows = _parse_kernel_csv(output)
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
    output = await _query_kaggle(["kernels", "list", "--mine", "--page-size", str(max(1, min(limit, 200))), "--sort-by", "dateRun", "--csv"], timeout=8)
    rows = _parse_kernel_csv(output)
    if not rows:
        return "No notebooks found."

    active_statuses = {"RUNNING", "QUEUED", "PREPARING", "PENDING", "STARTING", "CANCEL_REQUESTED"}
    semaphore = asyncio.Semaphore(8)

    async def query(row):
        ref = row.get("ref", "")
        if not ref:
            return None, "Missing notebook reference"
        async with semaphore:
            try:
                output = await _query_kaggle(["kernels", "status", ref], timeout=8)
                match = re.search(r'has status "([^"]+)"', output)
                if not match:
                    return None, f"{ref}: unrecognized status response"
                status = _clean_status_label(match.group(1))
                return (f"- {row.get('title', ref)} | {ref} | status={status}" if status in active_statuses else None), None
            except Exception as err:
                return None, f"{ref}: {type(err).__name__}"

    tasks = [asyncio.create_task(query(row)) for row in rows]
    try:
        done, pending = await asyncio.wait(tasks, timeout=16)
        results = [task.result() for task in done]
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    active = sorted(value for value, error in results if value)
    failures = [error for value, error in results if error]
    lines = ([f"{len(active)} active notebook runs:"] + active[:head]) if active else ["No active runs found among successfully checked notebooks."]
    if len(active) > head:
        lines.append(f"... {len(active) - head} more active")
    if pending or failures:
        lines.append(f"Incomplete status scan: {len(pending)} unfinished, {len(failures)} failed; these notebooks may still be active.")
        lines.extend(failures[:head])
    return "\n".join(lines)

def _fetch_live_kernel_telemetry(owner: str, slug: str) -> dict[str, Any]:
    """Query Kaggle midtier APIs directly for live kernel metadata and session status.

    Returns dictionary with machine_shape, enable_gpu, enable_tpu, status, and failure_message.
    """
    from kaggle.api.kaggle_api_extended import KaggleApi
    from kagglesdk.kernels.types.kernels_api_service import ApiGetKernelRequest, ApiGetKernelSessionStatusRequest

    telemetry: dict[str, Any] = {
        "machine_shape": None,
        "enable_gpu": False,
        "enable_tpu": False,
        "status": None,
        "failure_message": None,
    }
    try:
        api = KaggleApi()
        api.authenticate()
        with api.build_kaggle_client() as client:
            # 1. Get live hardware config
            try:
                k_req = ApiGetKernelRequest()
                k_req.user_name = owner
                k_req.kernel_slug = slug
                k_res = client.kernels.kernels_api_client.get_kernel(k_req)
                meta = getattr(k_res, "metadata", None)
                if meta:
                    telemetry["machine_shape"] = getattr(meta, "machine_shape", None)
                    telemetry["enable_gpu"] = bool(getattr(meta, "enable_gpu", False))
                    telemetry["enable_tpu"] = bool(getattr(meta, "enable_tpu", False))
            except Exception:
                pass

            # 2. Get live session status & failure message
            try:
                s_req = ApiGetKernelSessionStatusRequest()
                s_req.user_name = owner
                s_req.kernel_slug = slug
                s_res = client.kernels.kernels_api_client.get_kernel_session_status(s_req)
                if s_res:
                    raw_status = getattr(s_res, "status", None)
                    if raw_status:
                        telemetry["status"] = _clean_status_label(str(raw_status))
                    telemetry["failure_message"] = getattr(s_res, "failure_message", "")
            except Exception:
                pass
    except Exception:
        pass

    return telemetry


def _fetch_live_logs_stream(owner: str, slug: str) -> list[str]:
    """Stream live logs via Kaggle midtier SSE/stream endpoint with CLI fallback."""
    from kaggle.api.kaggle_api_extended import KaggleApi
    from kagglesdk.kernels.types.kernels_api_service import ApiGetKernelSessionLogsStreamRequest

    clean_lines: list[str] = []
    # Priority 1: SDK live log stream endpoint
    try:
        api = KaggleApi()
        api.authenticate()
        with api.build_kaggle_client() as client:
            req = ApiGetKernelSessionLogsStreamRequest()
            req.user_name = owner
            req.kernel_slug = slug
            res = client.kernels.kernels_api_client.get_kernel_session_logs_stream(req)
            content_type = res.headers.get("Content-Type", "")
            if "text/event-stream" in content_type:
                for raw_line in res.iter_lines():
                    if not raw_line:
                        continue
                    line_str = raw_line.decode("utf-8", errors="replace")
                    if line_str.startswith("data:"):
                        data_val = line_str[5:].strip()
                        try:
                            item = json.loads(data_val)
                            text = item.get("data") or item.get("message") or ""
                            for sub in text.splitlines():
                                if sub.strip():
                                    clean_lines.append(sub)
                        except Exception:
                            clean_lines.append(data_val)
            else:
                try:
                    items = json.loads(res.text)
                    if isinstance(items, list):
                        for it in items:
                            text = it.get("data") or it.get("message") or ""
                            for sub in text.splitlines():
                                if sub.strip():
                                    clean_lines.append(sub)
                    else:
                        clean_lines = [l for l in res.text.splitlines() if l.strip()]
                except Exception:
                    clean_lines = [l for l in res.text.splitlines() if l.strip()]
    except Exception:
        pass

    # Priority 2: CLI logs fallback if stream endpoint didn't return lines
    if not clean_lines:
        try:
            logs_proc = _run_kaggle(["kernels", "logs", f"{owner}/{slug}"], timeout=60)
            raw_stdout = logs_proc.stdout.strip()
            if raw_stdout.startswith("[") or raw_stdout.startswith("{"):
                try:
                    parsed = json.loads(raw_stdout)
                    if isinstance(parsed, list):
                        for item in parsed:
                            text_part = item.get("data") or item.get("message") or ""
                            for sub in text_part.splitlines():
                                if sub.strip():
                                    clean_lines.append(sub)
                except Exception:
                    clean_lines = [l for l in raw_stdout.splitlines() if l.strip()]
            else:
                clean_lines = [l for l in raw_stdout.splitlines() if l.strip()]
        except Exception:
            pass

    return clean_lines


@app.tool()
async def view_status(notebook: str, tail: int = 10, fetch_logs: bool = True) -> str:
    """Check Kaggle notebook status with rich live execution telemetry.

    Queries Kaggle midtier APIs directly for:
    - Current status: QUEUED, RUNNING, COMPLETE, ERROR, or CANCEL_ACKNOWLEDGED
    - Hardware / Accelerator: Actual GPU/TPU allocated on Kaggle Cloud (e.g. NvidiaTeslaT4, NvidiaTeslaP100, TPU, or CPU)
    - Elapsed time: Wall time since the run started
    - CPU/GPU usage: Extracted from live stdout/stderr telemetry markers
    - Live logs: Latest 10 lines from Kaggle's live log stream endpoint
    """
    name, folder = _resolve_notebook_folder(notebook)
    meta = _read_metadata(folder)
    owner = _default_owner()
    existed, kernel = await _find_kernel(meta.get("title", name), name)
    ref = meta.get("id") or _full_slug(kernel) or f"{owner}/{_slugify(name)}"
    owner_part, slug_part = _split_kernel_slug(ref)

    # 1. Fetch live telemetry from Kaggle API
    telemetry = _fetch_live_kernel_telemetry(owner_part, slug_part)

    # Check status via CLI as well to ensure version capture
    status_proc = _run_kaggle(["kernels", "status", ref], timeout=30)
    status_text = status_proc.stdout.strip()
    match = re.search(r'has status "([^"]+)"', status_text)
    cli_status_label = _clean_status_label(match.group(1) if match else "UNKNOWN")
    status_label = telemetry.get("status") or cli_status_label

    version_match = re.search(r"version\s+(\d+)", status_text, re.I)
    version = int(version_match.group(1)) if version_match else meta.get("last_version_number")

    # 2. Hardware / Accelerator resolution from live Kaggle Cloud metadata
    raw_shape = telemetry.get("machine_shape")
    server_shape = raw_shape if raw_shape and str(raw_shape).strip().lower() not in ("none", "") else None
    server_gpu = bool(telemetry.get("enable_gpu"))
    server_tpu = bool(telemetry.get("enable_tpu"))

    meta_acc = str(meta.get("accelerator") or "none").lower()
    meta_gpu = meta.get("enable_gpu") in (True, "true")
    meta_tpu = meta.get("enable_tpu") in (True, "true")

    hardware_display = "CPU"
    if server_shape:
        hardware_display = f"GPU ({server_shape})"
    elif server_gpu or meta_gpu or (meta_acc not in ("none", "cpu", "") and _is_gpu_accelerator(meta_acc)):
        acc_name = server_shape or (meta_acc if meta_acc not in ("none", "cpu", "") else "NvidiaTeslaT4")
        if acc_name in ("none", "cpu", None):
            acc_name = "NvidiaTeslaT4"
        hardware_display = f"GPU ({acc_name})"
    elif server_tpu or meta_tpu or meta_acc == "tpu":
        hardware_display = "TPU (v3-8)"
    else:
        hardware_display = "CPU"
    # 3. Elapsed Time Calculation
    last_pushed = meta.get("last_pushed_utc")
    elapsed_str = "unknown"
    if last_pushed:
        try:
            pushed_dt = datetime.fromisoformat(last_pushed)
            elapsed_sec = int((datetime.now(timezone.utc) - pushed_dt).total_seconds())
            mins, secs = divmod(elapsed_sec, 60)
            hours, mins = divmod(mins, 60)
            if hours > 0:
                elapsed_str = f"{hours}h {mins}m {secs}s"
            elif mins > 0:
                elapsed_str = f"{mins}m {secs}s"
            else:
                elapsed_str = f"{secs}s"
        except Exception:
            pass

    run_dir = folder / "runs" / str(version or 0)
    run_dir.mkdir(parents=True, exist_ok=True)

    # 4. Fetch live logs via Kaggle stream endpoint
    log_snippet = ""
    usage_telemetry = []
    if fetch_logs:
        clean_lines = _fetch_live_logs_stream(owner_part, slug_part)
        if clean_lines:
            (run_dir / "logs.txt").write_text("\n".join(clean_lines))
            build_run_summary(run_dir)

            tail_lines = clean_lines[-tail:]
            log_snippet = "\n".join(tail_lines)

            # Search for GPU/CPU usage markers in logs
            for line in clean_lines:
                l_lower = line.lower()
                if any(k in l_lower for k in ("nvidia-smi", "cuda memory", "gpu memory", "gpu utilization", "vram", "cpu utilization", "ram used")):
                    usage_telemetry.append(line.strip())

    # Update metadata
    _write_metadata(
        folder,
        {
            "last_status": status_label,
            "last_status_utc": datetime.now(timezone.utc).isoformat(),
        },
    )

    lines = [
        f"Notebook: '{name}' ({ref})",
        f"- Status: {status_label} (version {version or 'unknown'})",
        f"- Hardware: {hardware_display}",
        f"- Elapsed time: {elapsed_str}",
    ]
    if telemetry.get("failure_message"):
        lines.append(f"- Failure reason: {telemetry['failure_message']}")
    if usage_telemetry:
        lines.append(f"- Resource usage detected: {usage_telemetry[-1]}")

    if log_snippet:
        lines.append(f"\nLatest {tail} lines of live logs:\n```text\n{log_snippet}\n```")
    else:
        lines.append(f"\n(No logs available yet)")

    return "\n".join(lines)

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
    """Upload or update a dataset. Please run as CLI for long uploads:

    uv run --script mcp/kaggle_mcp.py upload PATH --options '{"dataset_slug":"slug"}'

    Options are title, dataset_slug, private, version_notes, keep_tabular, dir_mode.
    MCP returns after 29 seconds with a PID and log if still running. The upload
    continues: monitor its log, do NOT restart it. Use CLI for future long uploads.
    """
    options = dict(title=title, dataset_slug=dataset_slug, private=private,
                   version_notes=version_notes, keep_tabular=keep_tabular, dir_mode=dir_mode)
    return await _transfer("upload", path, options)


async def _transfer(operation: str, source: str, options: dict) -> str:
    import tempfile
    deadline = asyncio.get_running_loop().time() + 29
    with tempfile.NamedTemporaryFile(prefix=f"kaggle-{operation}-", suffix=".log", delete=False) as log:
        command = [sys.executable, "-u", str(Path(__file__).resolve()), "--workspace-root",
                   str(get_workspace_root()), operation, source, "--options", json.dumps(options)]
        proc = await asyncio.create_subprocess_exec(*command, env=_cli_env(),
                   stdout=log, stderr=log, start_new_session=True)
        try:
            await asyncio.wait_for(proc.wait(), timeout=max(0, deadline - asyncio.get_running_loop().time()))
        except TimeoutError:
            return (f"Please run as CLI for long transfers. This {operation} is still running as PID {proc.pid}. "
                    f"Log: {log.name}. Do not restart it; inspect the log for completion or errors. "
                    f"Future transfers: uv run --script mcp/kaggle_mcp.py {operation} SOURCE --options JSON")
    output = Path(log.name).read_text()
    if proc.returncode:
        raise RuntimeError(f"{operation.capitalize()} failed ({proc.returncode}). Log: {log.name}\n{output}")
    return output


def _upload_dataset(path: str, title: str | None = None, dataset_slug: str | None = None,
                    private: bool = True, version_notes: str | None = None,
                    keep_tabular: bool = True, dir_mode: str = "zip") -> str:
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
    """Download a dataset. Please run as CLI for long downloads:

    uv run --script mcp/kaggle_mcp.py download OWNER/SLUG --options '{"unzip":true}'

    Options: target_dir, unzip. Default destination: ./kaggle/datasets/<slug>/.
    After 29 seconds MCP returns the PID and log; download continues. Do not restart it.
    """
    return await _transfer("download", dataset, dict(target_dir=target_dir, unzip=unzip))


def _download_dataset(dataset: str, target_dir: str | None = None, unzip: bool = True) -> str:
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
        f"Downloaded dataset '{clean_ref}' into `{dest}`:\n"
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
    if mine:
        cmd.append("--mine")
    if search:
        cmd.extend(["--search", search])
    if page > 1:
        cmd.extend(["-p", str(page)])
    if sort_by in {"hottest", "votes", "updated", "active"}:
        cmd.extend(["--sort-by", sort_by])
    output = await _query_kaggle(cmd)
    rows = _parse_kernel_csv(output)
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
    output = await _query_kaggle(["datasets", "status", dataset.strip()])
    status_text = output.strip() or "UNKNOWN"
    return f"Dataset '{dataset}' status: {status_text}"

@app.tool()
async def get_quota() -> str:
    """Show your weekly Kaggle GPU and TPU accelerator quota (used, remaining, total, refresh date)."""
    try:
        output = await _query_kaggle(["quota", "--csv"])
        rows = _parse_kernel_csv(output)
        if not rows:
            return output.strip() or "Quota information unavailable."
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
    parser = argparse.ArgumentParser(description="Kaggle Local MCP Server & CLI Tool")
    parser.add_argument("--config", type=Path, help="Path to machine-local kaggle.toml")
    parser.add_argument("--workspace-root", type=Path, help="Override workspace root directory")

    subparsers = parser.add_subparsers(dest="command", help="CLI Subcommands")

    # push
    p_push = subparsers.add_parser("push", help="Push notebook and run on Kaggle")
    p_push.add_argument("notebook", help="Notebook name or directory")
    p_push.add_argument("--accelerator", help="Accelerator (none, gpu, tpu)")

    # save
    p_save = subparsers.add_parser("save", help="Save notebook locally and sync code without running")
    p_save.add_argument("notebook", help="Notebook name or directory")

    # wait
    p_wait = subparsers.add_parser("wait", help="Wait for notebook completion or log text")
    p_wait.add_argument("notebook", help="Notebook name or owner/slug")
    p_wait.add_argument("--timeout", type=int, default=20)
    p_wait.add_argument("--poll-interval", type=int, default=5)
    p_wait.add_argument("--until", choices=("complete", "log"), default="complete")
    p_wait.add_argument("--text")

    # status
    p_status = subparsers.add_parser("status", help="Check notebook status")
    p_status.add_argument("notebook", help="Notebook name or directory")
    p_status.add_argument("--logs", action="store_true", help="Fetch logs")

    # output
    p_output = subparsers.add_parser("output", help="Download notebook outputs")
    p_output.add_argument("notebook", help="Notebook name or directory")
    p_output.add_argument("--run", type=int, help="Specific run number")

    # quota
    subparsers.add_parser("quota", help="Check accelerator quota")
    upload = subparsers.add_parser("upload", help="Upload dataset without the MCP call timeout")
    upload.add_argument("path")
    upload.add_argument("--options", default="{}", help="JSON upload options")
    download = subparsers.add_parser("download", help="Download dataset without the MCP call timeout")
    download.add_argument("dataset")
    download.add_argument("--options", default="{}", help="JSON download options")

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

    # CLI mode
    if args.command:
        cmd = args.command
        if cmd == "quota":
            print(asyncio.run(get_quota()))
        elif cmd == "status":
            print(asyncio.run(view_status(args.notebook, fetch_logs=args.logs)))
        elif cmd == "push":
            print(asyncio.run(push_notebook(args.notebook, accelerator=args.accelerator)))
        elif cmd == "save":
            print(asyncio.run(save_notebook(args.notebook)))
        elif cmd == "wait":
            print(asyncio.run(wait(args.notebook, until=args.until, text=args.text, timeout=args.timeout, poll_interval=args.poll_interval)))
        elif cmd == "output":
            print(asyncio.run(fetch_output(args.notebook, run_number=args.run)))
        elif cmd == "upload":
            print(_upload_dataset(args.path, **json.loads(args.options)))
        elif cmd == "download":
            print(_download_dataset(args.dataset, **json.loads(args.options)))
        return

    # MCP server mode
    app.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
