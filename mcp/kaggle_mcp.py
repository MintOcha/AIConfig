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
   - NEVER poll `view_notebook` in tight loops.
   - Use `wait_for_notebook(notebook="owner/slug", until="complete", timeout=20)` or `until="log", text="epoch 1"`.
   - Longer waits require a matching client timeout; output download is explicit via pull_outputs.
   - When finished, `pull_outputs` retrieves artifacts and compressed summaries.
"""
)

def _format_result(value: str) -> str:
    try:
        data = json.loads(value)
    except (ValueError, TypeError):
        return value
    if isinstance(data, dict) and "changes" not in data and ("ref" in data or "title" in data):
        fields = ("ref", "title", "subtitle", "visibility", "isPrivate", "currentVersionNumber",
                  "version", "status", "status_error", "licenseName", "totalBytes", "lastUpdated",
                  "nextPageToken", "next_page_token", "error", "error_message",
                  "instances", "author", "slug", "description")
        data = {key: data[key] for key in fields if key in data}
    def render(item, prefix=""):
        if isinstance(item, dict):
            rows = []
            for key, child in item.items():
                rows.extend(render(child, f"{prefix}.{key}" if prefix else key))
            return rows
        if isinstance(item, list):
            rows = []
            for index, child in enumerate(item[:10]):
                rows.extend(render(child, f"{prefix}[{index + 1}]"))
            if len(item) > 10:
                rows.append(f"{prefix}: {len(item) - 10} more entries omitted; use pagination")
            return rows or [f"{prefix}: none"]
        text = "unknown" if item is None else str(item)
        if len(text) > 400:
            text = text[:397] + "... [truncated]"
        return [f"{prefix}: {text}" if prefix else text]
    return "\n".join(render(data))


from fastmcp.server.middleware import Middleware
from fastmcp.tools.tool import ToolResult


class ResultPresentation(Middleware):
    async def on_list_tools(self, context, call_next):
        tools = await call_next(context)
        return [tool.model_copy(update={"output_schema": None}) for tool in tools]

    async def on_call_tool(self, context, call_next):
        result = await call_next(context)
        for block in result.content:
            if block.type == "text":
                block.text = _format_result(block.text)
        return ToolResult(content=result.content, meta=result.meta, is_error=result.is_error)


app.add_middleware(ResultPresentation())


def _print_result(value: str) -> None:
    print(_format_result(value))


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
    try:
        proc = subprocess.run(
            cmd,
            cwd=get_workspace_root(),
            env=_cli_env(),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = _strip_cli_warnings(exc.stderr or "")
        stdout = _strip_cli_warnings(exc.stdout or "")
        err_msg = stderr.strip() or stdout.strip() or f"Process exited with code {exc.returncode}"
        raise RuntimeError(f"Kaggle command failed ({exc.returncode}): {' '.join(cmd)}\n{err_msg}") from None
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


def _register_search(resource: str) -> None:
    async def search(query: str, page: int = 1, page_token: str | None = None,
                     owner: str | None = None, sort_by: str | None = None) -> str:
        return await search_library(query, resource, page, page_token, owner, sort_by)
    search.__doc__ = f"Search Kaggle {resource} without downloading or changing anything. Models use page_token instead of page."
    app.tool(name=f"search_{resource}")(search)


for _resource in ("datasets", "notebooks", "models", "competitions"):
    _register_search(_resource)


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


async def read_metadata(reference: str, kind: str = "datasets") -> str:
    """Read dataset or model metadata by owner/slug without downloading files or weights."""
    if kind not in {"datasets", "models"}:
        raise ValueError("kind must be datasets or models")
    if not re.fullmatch(r"[\w-]+/[\w.-]+", reference):
        raise ValueError("Expected owner/slug")
    code = '''
import sys, json
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
    metadata = result.to_dict()
    if kind == 'datasets':
        metadata['isPrivate'] = result.is_private
        metadata['visibility'] = 'private' if result.is_private else 'public'
    print(json.dumps(metadata))
'''
    return await _query_process([sys.executable, "-c", code, kind, reference])


@app.tool(name="view_dataset")
async def read_dataset(dataset: str) -> str:
    """Read dataset details, explicit visibility, and upload processing state."""
    details, status = await asyncio.gather(read_metadata(dataset, "datasets"),
                                         _query_kaggle(["datasets", "status", dataset]),
                                         return_exceptions=True)
    if isinstance(details, BaseException):
        raise details
    result = json.loads(details)
    if isinstance(status, BaseException):
        result["status_error"] = str(status)
    else:
        result["status"] = status.strip()
    return json.dumps(result)


@app.tool(name="view_model")
async def read_model(model: str) -> str:
    """Read model details and available variations without downloading weights."""
    return await read_metadata(model, "models")


@app.tool()
async def create_model(path: str) -> str:
    """Create a model from local model-metadata.json, including its model card and privacy.
    Does not upload weights. Use push_model to create variations and versions.
    """
    folder = Path(path)
    if not folder.is_absolute():
        folder = get_workspace_root() / folder
    return await _query_kaggle(["models", "create", "-p", str(folder)])


@app.tool()
async def edit_model(path: str, variation: bool = False, changes: dict[str, Any] | None = None) -> str:
    """Publish a complete local model-metadata.json or model-instance-metadata.json,
    or apply in-place changes (title, subtitle, description, isPrivate) to a model directly.
    Pull existing metadata with Kaggle models get before editing to preserve settings.
    Model cards describe research; variation metadata supplies usage and licensing.
    Does not upload weights or run notebooks.
    """
    folder = Path(path)
    if not folder.is_absolute():
        folder = get_workspace_root() / folder
    if changes:
        meta_file = folder / ("model-instance-metadata.json" if variation else "model-metadata.json")
        if meta_file.exists():
            data = json.loads(meta_file.read_text())
        else:
            data = {}
        data.update(changes)
        meta_file.write_text(json.dumps(data, indent=2))
    return await _query_kaggle(["models", *(["instances"] if variation else []),
                                "update", "-p", str(folder)])

@app.tool()
async def push_model(path: str, variation: str | None = None, version_notes: str = "",
                     dir_mode: str = "zip") -> str:
    """Upload model weights. Omit variation to create one using model-instance-metadata.json.
    Supply owner/model/framework/variation to publish a new version of an existing variation.
    Create the parent model with create_model first. Files must be in a local directory.
    After 29 seconds returns PID/log while upload continues; do not restart.
    CLI: kaggle_mcp.py push-model PATH --options JSON.
    """
    return await _transfer("push-model", path, dict(variation=variation,
                           version_notes=version_notes, dir_mode=dir_mode))


@app.tool()
async def pull_model(model: str, target_dir: str) -> str:
    """Download weights for owner/model/framework/variation/version to target_dir.
    After 29 seconds returns PID/log while download continues; do not restart.
    CLI: kaggle_mcp.py pull-model HANDLE --options '{"target_dir":"path"}'.
    """
    return await _transfer("pull-model", model, dict(target_dir=target_dir))


@app.tool()
async def list_model_versions(variation: str, page_token: str | None = None, page_size: int = 20) -> str:
    """List native version records for a variation. Returns Kaggle pagination tokens."""
    if not 1 <= page_size <= 200:
        raise ValueError("page_size must be 1–200")
    command = ["models", "instances", "versions", "list", variation,
               "--format", "json", "--page-size", str(page_size)]
    if page_token:
        command.extend(["--page-token", page_token])
    return await _query_kaggle(command)


@app.tool()
async def list_model_files(variation: str) -> str:
    """List files in a model variation (owner/model/framework/variation or owner/model/framework/variation/version)."""
    parts = variation.strip().strip("/").split("/")
    if len(parts) == 4:
        code = '''
import sys, json
from kaggle.api.kaggle_api_extended import KaggleApi
api = KaggleApi(); api.authenticate()
variation = sys.argv[1]
try:
    res = api.model_instance_get(variation)
    ver = res.get("versionNumber", 1) if isinstance(res, dict) else getattr(res, "version_number", 1)
except Exception:
    ver = 1
files_res = api.model_instance_version_files(f"{variation}/{ver}")
print(files_res.to_json() if hasattr(files_res, "to_json") else json.dumps(files_res.to_dict()))
'''
        return await _query_process([sys.executable, "-c", code, variation])
    elif len(parts) == 5:
        code = '''
import sys, json
from kaggle.api.kaggle_api_extended import KaggleApi
api = KaggleApi(); api.authenticate()
files_res = api.model_instance_version_files(sys.argv[1])
print(files_res.to_json() if hasattr(files_res, "to_json") else json.dumps(files_res.to_dict()))
'''
        return await _query_process([sys.executable, "-c", code, variation])
    else:
        raise ValueError("Expected variation handle: owner/model/framework/variation or owner/model/framework/variation/version")
@app.tool()
async def pull_model_metadata(model: str, target_dir: str, variation: bool = False) -> str:
    """Retrieve editable model or variation metadata without downloading weights.
    Edit the generated JSON and use edit_model to publish it. Preserve privacy settings.
    """
    target = Path(target_dir)
    if not target.is_absolute():
        target = get_workspace_root() / target
    target.mkdir(parents=True, exist_ok=True)
    return await _query_kaggle(["models", *(["instances"] if variation else []),
                               "get", model, "-p", str(target)])


@app.tool()
async def delete_model(model: str) -> str:
    """Permanently delete a model (owner/model), variation (four parts), or version (five).
    Deletes the selected resource and its children. Use only with explicit authorization.
    """
    parts = model.split("/")
    if len(parts) not in (2, 4, 5) or not all(parts):
        raise ValueError("Expected model, variation, or version handle")
    group = ["models"] + (["instances"] if len(parts) >= 4 else [])
    if len(parts) == 5:
        group.append("versions")
    return await _query_kaggle([*group, "delete", model, "--yes"])


def _model_transfer(operation: str, source: str, options: dict) -> str:
    if operation == "push-model":
        folder = Path(source)
        if not folder.is_absolute():
            folder = get_workspace_root() / folder

        meta_file = folder / "model-instance-metadata.json"
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text())
                changed = False
                if "trainingData" in meta and isinstance(meta["trainingData"], list):
                    cleaned_td = []
                    for item in meta["trainingData"]:
                        if isinstance(item, dict):
                            slug = item.get("datasetSlug") or item.get("slug") or str(item)
                            cleaned_td.append(str(slug))
                            changed = True
                        else:
                            cleaned_td.append(str(item))
                    meta["trainingData"] = cleaned_td

                valid_types = {
                    "unspecified": "Unspecified",
                    "basemodel": "BaseModel",
                    "base_model": "BaseModel",
                    "kagglevariant": "KaggleVariant",
                    "kaggle_variant": "KaggleVariant",
                    "externalvariant": "ExternalVariant",
                    "external_variant": "ExternalVariant",
                }
                curr_type = meta.get("modelInstanceType")
                has_external = bool(meta.get("externalBaseModelUrl"))
                if curr_type and isinstance(curr_type, str):
                    key = curr_type.lower().replace("-", "").replace("_", "")
                    default_type = "ExternalVariant" if has_external else "Unspecified"
                    normalized = valid_types.get(key, default_type)
                    if normalized != curr_type:
                        meta["modelInstanceType"] = normalized
                        changed = True
                elif has_external and curr_type == "Unspecified":
                    meta["modelInstanceType"] = "ExternalVariant"
                    changed = True
                if changed:
                    meta_file.write_text(json.dumps(meta, indent=2))
            except Exception:
                pass

        variation = options.get("variation")
        command = ["models", "instances"]
        if variation:
            command.extend(["versions", "create", variation, "-n", options.get("version_notes", "")])
        else:
            command.append("create")
        command.extend(["-p", str(folder), "-r", options.get("dir_mode", "zip")])
    else:
        target = Path(options["target_dir"])
        if not target.is_absolute():
            target = get_workspace_root() / target
        command = ["models", "instances", "versions", "download", source, "-p", str(target)]
    result = _run_kaggle(command, timeout=3600)
    return result.stdout or result.stderr or "Success"


@app.tool(name="edit_dataset")
async def update_dataset_metadata(dataset: str, changes: dict[str, Any]) -> str:
    """Edit dataset presentation without uploading files. Preserves unspecified metadata.

    Fields: title, subtitle, description (Markdown), licenses, keywords,
    expectedUpdateFrequency, userSpecifiedSources. Dates and author are managed by Kaggle.
    Licenses use [{"name":"LICENSE-ID"}]. isPrivate changes visibility; publishing requires explicit user authorization. Collaborators are preserved.
    """
    allowed = {"title", "subtitle", "description", "licenses", "keywords",
               "expectedUpdateFrequency", "userSpecifiedSources", "isPrivate",
               "image", "imageUrl", "image_url"}
    if not changes or changes.keys() - allowed:
        raise ValueError(f"Supply presentation fields only: {sorted(allowed)}")
    if "isPrivate" in changes and not isinstance(changes["isPrivate"], bool):
        raise ValueError('isPrivate must be a boolean')
    if not re.fullmatch(r"[\w-]+/[\w.-]+", dataset):
        raise ValueError("Expected owner/slug")
    code = '''
import json, sys
from kaggle.api.kaggle_api_extended import KaggleApi
from kagglesdk.datasets.types.dataset_api_service import (
    ApiGetDatasetMetadataRequest, ApiUpdateDatasetMetadataRequest, DatasetSettings)
api = KaggleApi(); api.authenticate()
dataset, changes = sys.argv[1], json.loads(sys.argv[2])
with api.build_kaggle_client() as client:
    request = ApiGetDatasetMetadataRequest()
    request.owner_slug, request.dataset_slug = dataset.split('/')
    response = client.datasets.dataset_api_client.get_dataset_metadata(request)
    if response.error_message:
        raise RuntimeError(response.error_message)
    info = response.info
    if info is None:
        raise RuntimeError('Dataset metadata response omitted settings')
    settings = DatasetSettings()
    for name in ('title', 'subtitle', 'description', 'is_private', 'keywords',
                 'licenses', 'collaborators', 'data', 'expected_update_frequency',
                 'user_specified_sources'):
        setattr(settings, name, getattr(info, name))
    image_src = changes.get("imageUrl") or changes.get("image_url") or changes.get("image")
    if image_src:
        import urllib.request, tempfile, mimetypes
        from pathlib import Path
        temp_dir = tempfile.mkdtemp()
        if image_src.startswith(("http://", "https://")):
            req = urllib.request.Request(image_src, headers={"User-Agent": "Mozilla/5.0"})
            data = urllib.request.urlopen(req, timeout=30).read()
            ext = mimetypes.guess_extension(urllib.request.urlopen(req).headers.get_content_type()) or ".png"
            if ext not in (".png", ".jpg", ".jpeg", ".webp"):
                ext = ".png"
            local_img = Path(temp_dir) / f"cover{ext}"
            local_img.write_bytes(data)
        else:
            src_path = Path(image_src)
            local_img = Path(temp_dir) / src_path.name
            local_img.write_bytes(src_path.read_bytes())
        cropped_upload = api._upload_dataset_image_file(temp_dir, local_img.name)
        if cropped_upload:
            settings.image = cropped_upload

    clean_changes = {k: v for k, v in changes.items() if k not in ("image", "imageUrl", "image_url")}
    patch = DatasetSettings.from_dict(clean_changes)
    names = {'expectedUpdateFrequency': 'expected_update_frequency',
             'userSpecifiedSources': 'user_specified_sources', 'isPrivate': 'is_private'}
    for key in clean_changes:
        name = names.get(key, key)
        setattr(settings, name, getattr(patch, name))
    update = ApiUpdateDatasetMetadataRequest()
    update.owner_slug, update.dataset_slug = request.owner_slug, request.dataset_slug
    update.settings = settings
    result = client.datasets.dataset_api_client.update_dataset_metadata(update)
    if getattr(result, 'error_message', None):
        raise RuntimeError(result.error_message)
    report = dict(dataset=dataset, update_accepted=True, verification='unverified')
    before = json.loads(info.to_json())
    try:
        readback = client.datasets.dataset_api_client.get_dataset_metadata(request)
        if readback.error_message or readback.info is None:
            raise RuntimeError(readback.error_message or 'Readback omitted metadata')
        after = json.loads(readback.info.to_json())
        expected = json.loads(settings.to_json())
        fields = {key: dict(before=before.get(key), after=after.get(key), expected=expected.get(key),
                           verified=after.get(key) == expected.get(key)) for key in clean_changes}
        if image_src:
            fields['image'] = dict(updated=True, verified=True)
        report.update(verification='verified' if all(field['verified'] for field in fields.values()) else 'mismatch',
                      changes=fields, visibility='private' if after.get('isPrivate') else 'public',
                      ref=dataset)
        from kagglesdk.datasets.types.dataset_api_service import ApiGetDatasetRequest
        detail_request = ApiGetDatasetRequest()
        detail_request.owner_slug, detail_request.dataset_slug = dataset.split('/')
        detail = client.datasets.dataset_api_client.get_dataset(detail_request)
        report['version'] = detail.current_version_number
    except Exception as error:
        report['verification_error'] = str(error)
    print(json.dumps(report))
'''
    return await _query_process([sys.executable, "-c", code, dataset, json.dumps(changes)])


@app.tool()
async def delete_dataset(dataset: str) -> str:
    """Permanently delete a Kaggle dataset and its versions. Use only when explicitly authorized."""
    if not re.fullmatch(r"[\w-]+/[\w.-]+", dataset):
        raise ValueError("Expected owner/slug")
    return await _query_kaggle(["datasets", "delete", dataset, "--yes"])


@app.tool(name="edit_notebook")
async def update_notebook_presentation(notebook: str, title: str | None = None,
                                       markdown_path: str | None = None) -> str:
    """Publish notebook title and introductory Markdown using Kaggle Quick Save.

    Never executes the notebook. Fetches current remote source and settings,
    preserving code, embedded outputs, privacy and attached inputs. notebook may
    be owner/slug or a local notebook folder. markdown_path supplies introductory
    Markdown; it does not replace code. Existing run artifacts are not downloaded.
    """
    if title is None and markdown_path is None:
        raise ValueError("Provide title or markdown_path")
    if title is not None and len(title.strip()) < 5:
        raise ValueError("Title must be at least five characters")
    raw = Path(notebook)
    if "/" in notebook and not raw.exists() and not notebook.startswith("/"):
        ref = notebook.strip("/")
    else:
        _, folder = _resolve_notebook_folder(notebook)
        metadata = _read_metadata(folder)
        ref = metadata.get("id") or metadata.get("slug")
    if not ref or len(ref.split("/")) != 2:
        raise ValueError("An existing notebook reference owner/slug is required")
    markdown = None
    if markdown_path is not None:
        source = Path(markdown_path)
        if not source.is_absolute():
            source = get_workspace_root() / source
        markdown = source.read_text()
    code = '''
import sys, json
from kaggle.api.kaggle_api_extended import KaggleApi
from kagglesdk.kernels.types.kernels_api_service import ApiGetKernelRequest, ApiSaveKernelRequest
from kagglesdk.kernels.types.kernels_enums import KernelExecutionType
ref, title, markdown = json.loads(sys.argv[1])
api = KaggleApi(); api.authenticate()
with api.build_kaggle_client() as client:
    lookup = ApiGetKernelRequest()
    lookup.user_name, lookup.kernel_slug = ref.split('/')
    current = client.kernels.kernels_api_client.get_kernel(lookup)
    metadata = current.metadata
    text = current.blob.source
    if markdown is not None:
        if metadata.kernel_type != 'notebook':
            raise ValueError('Markdown presentation requires an ipynb notebook')
        document = json.loads(text)
        cell = dict(cell_type='markdown', metadata={'tags': ['kaggle-description']}, source=markdown)
        cells = document['cells']
        previous = next((i for i, item in enumerate(cells)
                         if 'kaggle-description' in item.get('metadata', {}).get('tags', [])), None)
        if previous is None:
            cells.insert(0, cell)
        else:
            cells[previous] = cell
        text = json.dumps(document)
    request = ApiSaveKernelRequest()
    request.id = metadata.id
    request.slug = metadata.ref
    request.new_title = title if title is not None else metadata.title
    request.text = text
    for field in ('language', 'kernel_type', 'is_private', 'enable_gpu', 'enable_tpu',
                  'enable_internet', 'dataset_data_sources', 'kernel_data_sources',
                  'competition_data_sources', 'model_data_sources', 'category_ids',
                  'docker_image', 'machine_shape'):
        setattr(request, field, getattr(metadata, field))
    request.kernel_execution_type = KernelExecutionType.QUICK_SAVE
    response = client.kernels.kernels_api_client.save_kernel(request)
    if response.error:
        raise RuntimeError(response.error)
    print(json.dumps({'notebook': response.ref.strip('/').removeprefix('code/'), 'title': request.new_title,
                      'execution_type': 'QUICK_SAVE', 'response': response.to_dict()}))
'''
    result = json.loads(await _query_process([sys.executable, "-c", code,
                                             json.dumps([ref, title, markdown])], timeout=90))
    for candidate in get_kaggle_root().iterdir():
        if candidate.is_dir() and _read_metadata(candidate).get("id") == ref:
            _write_metadata(candidate, {"id": result["notebook"], "slug": result["notebook"],
                                        "title": result["title"]})
    return json.dumps(result)


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
    Dataset ZIP contents may be expanded by Kaggle at mount time; inspect /kaggle/input
    and use the mounted files rather than assuming an uploaded source.zip survives.
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
                {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": _as_cell_source(f"import sys, subprocess\nsubprocess.run([sys.executable, '{script_name}'], check=True)\n")},
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
    Dataset archives may mount as extracted directories/files, not as the uploaded ZIP.
    Inspect the mounted input tree before loading; only unzip when the archive exists.
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

    # Extract canonical ref and URL from CLI output if available
    url_match = re.search(r"https?://(?:www\.)?kaggle\.com/(?:code/)?([^/\s]+/[^/\s]+)", result.stdout)
    if url_match:
        canonical_ref = url_match.group(1)
    else:
        canonical_ref = ref

    # Update metadata tracking
    owner_slug, kernel_slug = _split_kernel_slug(canonical_ref)
    current_runs = int(full_meta.get("total_runs", 0)) + 1
    meta_updates = {
        "id": canonical_ref,
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
        f"slug: {canonical_ref}\n"
        f"version: {version or 'latest'} (total runs: {current_runs})\n"
        f"accelerator: {acc} (gpu={full_meta.get('enable_gpu')}, tpu={full_meta.get('enable_tpu')})\n"
        f"internet: {full_meta.get('enable_internet')}"
        f"{attached_msg}\n"
        f"Notebook successfully queued on Kaggle.\n\n"
        f"[Next Step: To monitor execution, use CLI in background without blocking:\n"
        f"uv run --script /home/nas/Projects/AIConfig/mcp/kaggle_mcp.py wait {canonical_ref} --timeout 3600 --poll-interval 30\n"
        f"Or view live logs:\n"
        f"uv run --script /home/nas/Projects/AIConfig/mcp/kaggle_mcp.py status {canonical_ref} --logs --log-timeout 60]"
    )

ACTIVE_RUN_STATUSES = {"RUNNING", "QUEUED", "PREPARING", "PENDING", "STARTING", "CANCEL_REQUESTED"}
TERMINAL_RUN_STATUSES = {"COMPLETE", "CANCEL_ACKNOWLEDGED", "CANCELLED", "CANCELED", "ERROR", "FAILED"}


def _notebook_ref(notebook: str) -> str:
    value = notebook.strip().removeprefix("https://www.kaggle.com/code/").rstrip("/")
    if re.fullmatch(r"[\w-]+/[\w.-]+", value):
        return value
    _, folder = _resolve_notebook_folder(value)
    ref = _read_metadata(folder).get("id")
    if not ref:
        owner = _default_owner()
        ref = f"{owner}/{_slugify(value)}" if owner else None
    if not ref:
        raise ValueError("Pass a notebook reference owner/slug or a configured local notebook")
    return ref


async def _discover_runs(ref: str, version: int | None = None) -> list[dict]:
    if version is not None:
        return [await _run_snapshot(ref, version)]
    code = '''
import sys, json
from concurrent.futures import ThreadPoolExecutor
from kaggle.api.kaggle_api_extended import KaggleApi
from kagglesdk.kernels.types.kernels_api_service import ApiGetKernelRequest, ApiGetKernelSessionStatusRequest
api = KaggleApi(); api.authenticate()
owner, slug = sys.argv[1].split('/')
with api.build_kaggle_client() as client:
    request = ApiGetKernelRequest()
    request.user_name, request.kernel_slug = owner, slug
    metadata = client.kernels.kernels_api_client.get_kernel(request).metadata
latest = metadata.current_version_number
if latest < 1:
    print(json.dumps([dict(version=None, status='NO_SAVED_VERSION', logs=[], logs_error=None)]))
else:
    def inspect(version):
        with api.build_kaggle_client() as client:
            request = ApiGetKernelSessionStatusRequest()
            request.user_name, request.kernel_slug, request.version_label = owner, slug, f'v{version}'
            status = client.kernels.kernels_api_client.get_kernel_session_status(request).status.name
            return dict(version=version, status=status, hardware=metadata.machine_shape or 'unknown',
                        logs=[], logs_error=None, training_outcome='unknown')
    with ThreadPoolExecutor(max_workers=8) as pool:
        runs = list(pool.map(inspect, range(latest, 0, -1)))
    active = {'RUNNING', 'QUEUED', 'PREPARING', 'PENDING', 'STARTING', 'CANCEL_REQUESTED'}
    print(json.dumps([run for run in runs if run['status'] in active] or runs[:1]))
'''
    try:
        return json.loads(await _query_process([sys.executable, "-c", code, ref], timeout=60))
    except asyncio.TimeoutError as error:
        raise RuntimeError(f"Run discovery timed out for {ref}; no complete active-run inventory is available") from error


@app.tool(name="wait_for_notebook")
async def wait(
    notebook: str,
    until: str = "complete",
    text: str | None = None,
    version: int | str | None = None,
    versions: list[int | str] | None = None,
    timeout: int = 600,
    poll_interval: int = 5,
) -> str:
    """Wait until notebook terminates or a literal string appears in available logs.

    until: complete or log. log requires text (case-sensitive literal substring).
    version: specific version to wait for (int or 'v3').
    versions: list of versions (e.g. ['v3', 'v4'] or [3, 4]); returns when the FIRST one settles.
    Existing log text also matches; this is not restricted to new log lines.
    Returns concise text with reason matched, terminal, or timeout. Terminal errors stop
    either wait mode. No output download. Default timeout is 600s (10 minutes).
    IMPORTANT: When choosing timeout, inspect initial training logs first to check reported
    ETA / epoch durations, or estimate total runtime (e.g. eta_training_seconds) and set
    timeout accordingly to avoid premature timeout returns.
    """
    if until not in {"complete", "log"} or (until == "log" and not text):
        raise ValueError("until must be complete or log; log requires nonempty text")
    if timeout < 1 or poll_interval < 1:
        raise ValueError("timeout and poll_interval must be positive")
    ref = _notebook_ref(notebook)

    # Parse targets
    target_versions: list[int] = []
    if version is not None:
        v_int = int(str(version).lstrip("vV"))
        if v_int < 1:
            raise ValueError("version must be a positive integer")
        target_versions.append(v_int)
    if versions is not None:
        for v in versions:
            v_int = int(str(v).lstrip("vV"))
            if v_int < 1:
                raise ValueError("versions must be positive integers")
            if v_int not in target_versions:
                target_versions.append(v_int)

    deadline = asyncio.get_running_loop().time() + timeout
    snapshot = dict(version=None, status="UNKNOWN", logs=[], logs_error="Not queried", training_outcome="unknown")
    cli_tip = f"\n\n[Tip: Kaggle is notoriously slow. Please switch to using CLI for reliable long waits/logs:\nuv run --script /home/nas/Projects/AIConfig/mcp/kaggle_mcp.py wait {ref} --until {until}{f' --text {text!r}' if text else ''} --timeout 3600\nor check logs with:\nuv run --script /home/nas/Projects/AIConfig/mcp/kaggle_mcp.py status {ref} --logs --log-timeout 60]"
    wait_deadline = asyncio.timeout(timeout)
    try:
        async with wait_deadline:
            if not target_versions:
                inferred = await _discover_runs(ref)
                target_versions = [run["version"] for run in inferred if run["version"] is not None]
                if not target_versions:
                    return _format_run(ref, inferred[0], [], reason="not_running")
            while True:
                rem_time = max(5, int(deadline - asyncio.get_running_loop().time()))
                # If target versions specified, check each in order
                v_to_check: list[int | None] = target_versions if target_versions else [snapshot.get("version")]
                for target_v in v_to_check:
                    snapshot = await _run_snapshot(ref, target_v, logs=until == "log", log_timeout=min(30, rem_time))
                    lines = snapshot["logs"]
                    matches = [index for index, line in enumerate(lines) if text and text in line]
                    if until == "log" and matches:
                        index = matches[-1]
                        context = lines[max(0, index - 2):index + 3]
                        return _format_run(ref, snapshot, context, reason="matched") + "\nLiteral log-text match; may include traceback/source text, not evidence of training progress."
                    status = snapshot.get("status")
                    if status in TERMINAL_RUN_STATUSES or status not in ACTIVE_RUN_STATUSES:
                        reason = "terminal" if status in TERMINAL_RUN_STATUSES else "not_running"
                        return _format_run(ref, snapshot, lines[-5:], reason=reason)
                await asyncio.sleep(min(poll_interval, max(0, deadline - asyncio.get_running_loop().time())))
    except TimeoutError as error:
        if not wait_deadline.expired():
            raise RuntimeError(f"Kaggle request timed out while waiting for {ref}; the {timeout}s wait deadline was not reached. Completion was not observed.") from error
        last_logs = snapshot.get("logs", [])
        return (_format_run(ref, snapshot, last_logs[-5:], reason="timeout")
                + f"\nWait deadline reached ({timeout}s); requested condition was not observed. Last observed status only; the remote run was not cancelled."
                + cli_tip)
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


@app.tool(name="list_notebooks")
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


@app.tool(name="list_notebook_runs")
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
                runs = await _discover_runs(ref)
                lines = [f"- {row.get('title', ref)} | {ref} | version={run['version']} | status={run['status']}"
                         for run in runs if run['status'] in active_statuses]
                return ("\n".join(lines) if lines else None), None
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
    active = sorted(line for value, error in results if value for line in value.splitlines())
    failures = [error for value, error in results if error]
    lines = ([f"{len(active)} active notebook runs:"] + active[:head]) if active else ["No active runs found among successfully checked notebooks."]
    if len(active) > head:
        lines.append(f"... {len(active) - head} more active")
    if pending or failures:
        lines.append(f"Incomplete status scan: {len(pending)} unfinished, {len(failures)} failed; these notebooks may still be active.")
        lines.extend(failures[:head])
    return "\n".join(lines)





def _format_run(ref: str, snapshot: dict, logs: list[str], reason: str | None = None) -> str:
    header = f"{ref} · version {snapshot.get('version') or 'unknown'} · {snapshot['status']}"
    if reason:
        header += f" · {reason}"
    lines = [header, f"Hardware: {snapshot.get('hardware', 'unknown')} | Training outcome: unknown"]
    for line in logs:
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            event = None
        if isinstance(event, dict):
            line = " ".join(f"{key}={value:.6g}" if isinstance(value, float) else f"{key}={value}" for key, value in event.items())
        lines.append(line if len(line) <= 600 else line[:597] + "...")
    if snapshot.get("logs_error"):
        lines.append(f"Logs: {snapshot['logs_error']}")
    return "\n".join(lines)


def _matches_log_line(pattern: re.Pattern[str], line: str) -> bool:
    if pattern.search(line):
        return True
    try:
        event = json.loads(line)
        if isinstance(event, dict):
            formatted = " ".join(f"{key}={value:.6g}" if isinstance(value, float) else f"{key}={value}" for key, value in event.items())
            return bool(pattern.search(formatted))
    except (ValueError, TypeError):
        pass
    return False


@app.tool(name="view_notebook")
async def view_status(
    notebook: str,
    tail: int = 10,
    fetch_logs: bool = True,
    grep: str | None = None,
    context: int = 3,
    log_timeout: int = 30,
    version: int | None = None,
) -> str:
    """Inspect actual remote version/status with bounded log collection. COMPLETE is notebook status, not training success.

    Omit version to report every active saved version, or latest if none is active.
    grep: regex matching raw or formatted logs. tail limits matching lines; context adds
    this many surrounding lines on each side (default 3), merging overlapping ranges.
    Filtering operates only on the recent bounded collection buffer (up to 200 lines collected within the snapshot window)
    and preserves chronological order. It does not search or retrieve unavailable historical logs.
    """
    if tail < 1:
        raise ValueError("tail must be positive")
    if context < 0:
        raise ValueError("context must be nonnegative")
    pattern: re.Pattern[str] | None = None
    if grep is not None:
        try:
            pattern = re.compile(grep)
        except re.error as err:
            raise ValueError(f"Invalid regex pattern for grep: {err}") from err

    ref = _notebook_ref(notebook)
    if version is None:
        runs = await _discover_runs(ref)
        if not (fetch_logs or grep):
            return "\n\n".join(_format_run(ref, run, []) for run in runs)
        reports = await asyncio.gather(*(
            view_status(ref, tail, fetch_logs, grep, context, log_timeout, run["version"])
            if run["version"] is not None else asyncio.sleep(0, result=_format_run(ref, run, []))
            for run in runs
        ))
        return "\n\n".join(reports)
    snapshot = await _run_snapshot(ref, version, logs=fetch_logs or bool(grep), log_timeout=log_timeout)
    raw_logs = snapshot.get("logs", [])
    if pattern is not None:
        matches = [index for index, line in enumerate(raw_logs) if _matches_log_line(pattern, line)][-tail:]
        if not matches and raw_logs:
            no_match_msg = f"No log lines matched pattern {grep!r} in available collection buffer ({len(raw_logs)} lines inspected)"
            if snapshot.get("logs_error"):
                snapshot["logs_error"] = f"{snapshot['logs_error']}; {no_match_msg}"
            else:
                snapshot["logs_error"] = no_match_msg
        indices = sorted({index for match in matches
                          for index in range(max(0, match - context), min(len(raw_logs), match + context + 1))})
        selected_logs = [raw_logs[index] for index in indices]
    else:
        selected_logs = raw_logs[-tail:]
    out = _format_run(ref, snapshot, selected_logs)
    if snapshot.get("logs_error"):
        out += "\nStatus is available; log retrieval was incomplete. Use fetch_logs=false for a status-only request."
    return out

async def _run_snapshot(ref: str, version: int | None = None, logs: bool = False, log_timeout: int = 30) -> dict:
    if version is not None and version < 1:
        raise ValueError("Run number must be a positive Kaggle version")
    if log_timeout < 1:
        raise ValueError("log_timeout must be positive")
    baseline = await _run_snapshot(ref, version) if logs else None
    proc_timeout = max(35, log_timeout + 15)
    code = '''
import sys, json, threading
from collections import deque
from kaggle.api.kaggle_api_extended import KaggleApi
from kagglesdk.kernels.types.kernels_api_service import ApiGetKernelRequest, ApiGetKernelSessionStatusRequest, ApiListKernelSessionOutputRequest
api = KaggleApi(); api.authenticate()
owner, slug = sys.argv[1].split('/')
with api.build_kaggle_client() as client:
    request = ApiGetKernelRequest()
    request.user_name, request.kernel_slug = owner, slug
    metadata = client.kernels.kernels_api_client.get_kernel(request).metadata
    latest = metadata.current_version_number
    version = int(sys.argv[2]) if sys.argv[2] else latest
    if not version or version < 1:
        raise RuntimeError('Cannot determine remote version')
    request = ApiGetKernelSessionStatusRequest()
    request.user_name, request.kernel_slug, request.version_label = owner, slug, f'v{version}'
    status = client.kernels.kernels_api_client.get_kernel_session_status(request).status.name
    result = dict(version=version, status=status, logs=[], logs_error=None, training_outcome='unknown')
    result['hardware'] = metadata.machine_shape or 'unknown'
    if sys.argv[3] == '1':
        lines = deque(maxlen=200)
        errors = []
        def collect():
            try:
                if version == latest:
                    for event in api.kernels_logs_stream(owner + '/' + slug):
                        lines.extend(str(event.get('data', '')).splitlines())
                else:
                    request = ApiListKernelSessionOutputRequest()
                    request.user_name, request.kernel_slug, request.version_label = owner, slug, f'v{version}'
                    body = client.kernels.kernels_api_client.list_kernel_session_output(request).log
                    try:
                        events = json.loads(body)
                        for event in events:
                            lines.extend(str(event.get('data', '')).splitlines())
                    except (ValueError, TypeError):
                        lines.extend((body or '').splitlines())
            except Exception as error:
                errors.append(str(error))
        limit_sec = int(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4].isdigit() else 30
        worker = threading.Thread(target=collect, daemon=True)
        worker.start(); worker.join(limit_sec)
        result['logs'] = list(lines)
        result['logs_error'] = '; '.join(errors) or (f'Live log stream reached bound ({limit_sec}s); Kaggle stream is slow' if worker.is_alive() else None)
        if not result['logs'] and not result['logs_error']:
            result['logs_error'] = 'Kaggle returned no log events for this version'
        if version == latest:
            check = ApiGetKernelRequest()
            check.user_name, check.kernel_slug = owner, slug
            if client.kernels.kernels_api_client.get_kernel(check).metadata.current_version_number != version:
                result['logs'] = []
                result['logs_error'] = 'Remote latest version changed during log retrieval; discarded ambiguous logs'
    print(json.dumps(result), flush=True)
'''
    try:
        return json.loads(await _query_process([sys.executable, "-c", code, ref, str(version) if version else "", "1" if logs else "0", str(log_timeout)], timeout=proc_timeout))
    except (asyncio.TimeoutError, RuntimeError) as error:
        if baseline is None:
            if isinstance(error, asyncio.TimeoutError):
                raise RuntimeError(f"Kaggle status request for {ref} timed out after {proc_timeout}s") from error
            raise
        baseline["logs_error"] = (
            f"Log request timed out after {proc_timeout}s; showing status captured before log retrieval"
            if isinstance(error, asyncio.TimeoutError)
            else f"Log request failed; showing status captured before log retrieval: {error}"
        )
        return baseline



class RunVersion(int):
    status: str

    def __new__(cls, version: int, status: str):
        instance = super().__new__(cls, version)
        instance.status = status
        return instance

    def __iter__(self):
        yield int(self)
        yield self.status


async def _completed_run(ref: str, version: int | None = None) -> RunVersion:
    snapshot = await _run_snapshot(ref, version)
    status = snapshot.get("status") or "UNKNOWN"
    ver = snapshot.get("version")
    if ver is None or ver < 1:
        raise RuntimeError(f"Cannot determine remote version for '{ref}'.")
    if status in ACTIVE_RUN_STATUSES:
        raise RuntimeError(
            f"Run {ver} is currently {status}; outputs cannot be downloaded while a run is active. No output directory created.\n"
            f"[Next Step: Wait for the run to complete or cancel it first:\n"
            f"- Wait: uv run --script /home/nas/Projects/AIConfig/mcp/kaggle_mcp.py wait {ref} --until complete --timeout 3600\n"
            f"- Cancel: cancel_notebook(notebook='{ref}') or uv run --script /home/nas/Projects/AIConfig/mcp/kaggle_mcp.py cancel {ref}]"
        )
    if status in {"UNKNOWN", "UNPUSHED"}:
        raise RuntimeError(
            f"Run {ver} has status {status}; cannot safely verify whether run is stopped. No output directory created.\n"
            f"[Next Step: Check notebook status via CLI: uv run --script /home/nas/Projects/AIConfig/mcp/kaggle_mcp.py status {ref}]"
        )
    if status not in TERMINAL_RUN_STATUSES:
        raise RuntimeError(
            f"Run {ver} has unrecognized status '{status}'; outputs require a stopped terminal run. No output directory created.\n"
            f"[Next Step: Check status via CLI: uv run --script /home/nas/Projects/AIConfig/mcp/kaggle_mcp.py status {ref}]"
        )
    return RunVersion(ver, status)


async def _download_run(
    ref: str,
    version: int,
    output_dir: Path,
    pattern: str | None = None,
    timeout: int = 1800,
) -> int:
    code = '''
import sys, os, tempfile, fnmatch
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import requests
from kaggle.api.kaggle_api_extended import KaggleApi
from kagglesdk.kernels.types.kernels_api_service import ApiListKernelSessionOutputRequest
api = KaggleApi(); api.authenticate()
owner, slug = sys.argv[1].split('/')
version_label = 'v' + sys.argv[2]
destination = Path(sys.argv[3])
pattern = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] else None
with api.build_kaggle_client() as client:
    request = ApiListKernelSessionOutputRequest()
    request.user_name, request.kernel_slug = owner, slug
    request.version_label = version_label
    request.page_size = 200
    files = []
    while True:
        response = client.kernels.kernels_api_client.list_kernel_session_output(request)
        files.extend(response.files or [])
        if not response.next_page_token:
            break
        request.page_token = response.next_page_token
    if pattern:
        files = [f for f in files if fnmatch.fnmatch(f.file_name, pattern)]
    if not files:
        print(0)
        sys.exit(0)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        staging = Path(temporary)
        session = requests.Session()
        def download_file(item):
            target = staging / item.file_name
            if not target.resolve().is_relative_to(staging.resolve()):
                raise RuntimeError(f'Unsafe output filename: {item.file_name}')
            target.parent.mkdir(parents=True, exist_ok=True)
            with session.get(item.url, stream=True, timeout=60) as transfer:
                transfer.raise_for_status()
                with target.open('wb') as stream:
                    for chunk in transfer.iter_content(1024 * 1024):
                        stream.write(chunk)
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(download_file, files))
        destination.mkdir(parents=True, exist_ok=True)
        for root, dirs, filenames in os.walk(staging):
            rel_dir = Path(root).relative_to(staging)
            target_sub = destination / rel_dir
            target_sub.mkdir(parents=True, exist_ok=True)
            for fn in filenames:
                src_file = Path(root) / fn
                dst_file = target_sub / fn
                os.replace(src_file, dst_file)
    print(len(files))
'''
    proc_timeout = max(300, timeout)
    out = await _query_process(
        [sys.executable, "-c", code, ref, str(version), str(output_dir), pattern or ""],
        timeout=proc_timeout,
    )
    lines = [line.strip() for line in out.strip().splitlines() if line.strip()]
    count_str = lines[-1] if lines else "0"
    return int(count_str) if count_str.isdigit() else 0

@app.tool(name="pull_outputs")
async def fetch_output(
    notebook: str,
    run_number: int | None = None,
    pattern: str | None = None,
    timeout: int = 1800,
) -> str:
    """Retrieve outputs from a stopped Kaggle version (COMPLETE, CANCEL_ACKNOWLEDGED, ERROR).
    
    run_number is the remote version, never a local counter.
    pattern is an optional fnmatch glob to download selectively (e.g. 'terminal-credit-output/*' or '*.pt').
    timeout is download deadline in seconds (default 1800 / 30 mins).
    """
    if re.fullmatch(r"[\w-]+/[\w.-]+", notebook):
        ref = notebook
        name = notebook.split("/", 1)[1]
    else:
        name, _, _ = _source_path(notebook)
        existed, kernel = await _find_kernel(name, name)
        ref = _full_slug(kernel)
        if not ref:
            owner = _default_owner()
            ref = f"{owner}/{_slugify(name)}" if owner else None
        if not ref:
            raise RuntimeError(f"Could not resolve Kaggle kernel for '{name}'.")

    version, status = await _completed_run(ref, run_number)
    target_dir = get_kaggle_root() / name / "runs" / str(version)
    output_dir = target_dir / "output"

    try:
        downloaded = await _download_run(ref, version, output_dir, pattern=pattern, timeout=timeout)
    except Exception as err:
        raise RuntimeError(f"Output download failed for version {version}: {err}") from err

    if downloaded == 0:
        return f"Empty output for '{name}' (version {version}, status {status}). No artifacts found."

    # 3. High-speed in-memory compression (generates summary.txt, tree.txt, logs.compressed.txt)
    summary_text = build_run_summary(target_dir)

    # Calculate downloaded size
    files = list(output_dir.rglob("*"))
    file_count = sum(1 for f in files if f.is_file())
    if not file_count:
        return f"Empty output for '{name}' (version {version}, status {status}). No artifacts found."
    total_size = sum(f.stat().st_size for f in files if f.is_file())

    return (
        f"Fetched run outputs for '{name}' into `{target_dir.relative_to(get_workspace_root())}`:\n"
        f"- Status: {status}\n"
        f"- Files downloaded: {file_count} ({human_size(total_size)})\n\n"
        f"{summary_text[:1000]}"
    )


@app.tool(name="cancel_notebook")
async def cancel_run(notebook: str, version: int | None = None, dry_run: bool = False) -> str:
    """Cancel a verified active notebook version, never an inferred numeric session ID.

    Omit version to select the sole active run. Multiple active runs require an
    explicit version. dry_run resolves and verifies the target without cancelling.
    """
    ref = _notebook_ref(notebook)
    runs = await _discover_runs(ref, version)
    active = [run for run in runs if run["status"] in ACTIVE_RUN_STATUSES]
    if not active:
        return f"{ref}: no active run to cancel."
    if len(active) != 1:
        raise ValueError(f"Multiple active runs for {ref}: {[r['version'] for r in active]}; specify version")
    target = active[0]["version"]
    code = '''
import sys, json, re
from urllib.parse import urlparse
from kaggle.api.kaggle_api_extended import KaggleApi
from kagglesdk.kernels.types.kernels_api_service import ApiGetKernelSessionStatusRequest, ApiCancelKernelSessionRequest
ref, version, dry_run = json.loads(sys.argv[1])
api = KaggleApi(); api.authenticate()
with api.build_kaggle_client() as client:
    http = client._http_client
    http._init_session()
    owner, slug = ref.split('/')
    response = http._session.get(
        f'https://api.kaggle.com/v1/kernels/output/download/{owner}/{slug}',
        params={'versionNumber': version}, stream=True, allow_redirects=False, timeout=20)
    try:
        location = urlparse(response.headers.get('Location', ''))
        match = re.fullmatch(r'/v1/kernels/output/download_zip/([1-9][0-9]*)', location.path)
        if response.status_code != 302 or location.scheme != 'https' or location.hostname != 'api.kaggle.com' or not match:
            raise RuntimeError(f'Cannot verify session identity for {ref} v{version}: HTTP {response.status_code}')
        session_id = int(match[1])
    finally:
        response.close()
    status_request = ApiGetKernelSessionStatusRequest()
    status_request.user_name, status_request.kernel_slug = owner, slug
    status_request.version_label = f'v{version}'
    status = client.kernels.kernels_api_client.get_kernel_session_status(status_request).status.name
    active = {'RUNNING', 'QUEUED', 'PREPARING', 'PENDING', 'STARTING', 'CANCEL_REQUESTED'}
    result = dict(notebook=ref, version=version, session_id=session_id, status=status, cancellation_sent=False)
    if status in active and not dry_run:
        request = ApiCancelKernelSessionRequest()
        request.kernel_session_id = session_id
        cancelled = client.kernels.kernels_api_client.cancel_kernel_session(request)
        if cancelled.error_message:
            raise RuntimeError(cancelled.error_message)
        result['cancellation_sent'] = True
    result['dry_run'] = dry_run
    print(json.dumps(result))
'''
    return await _query_process([sys.executable, "-c", code, json.dumps([ref, target, dry_run])], timeout=60)


@app.tool()
async def pull_notebook(notebook: str, fetch_latest_output: bool = False) -> str:
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

    snapshot = await _run_snapshot(ref)
    status_label, version = snapshot["status"], snapshot["version"]
    output_msg = ""
    if fetch_latest_output:
        version, status_label = await _completed_run(ref)
        run_dir = folder / "runs" / str(version)
        out_dir = run_dir / "output"
        try:
            downloaded = await _download_run(ref, version, out_dir)
            if downloaded > 0:
                build_run_summary(run_dir)
                output_msg = f"\nLatest output and compressed summary saved to `{run_dir.relative_to(get_workspace_root())}`."
            else:
                output_msg = f"\nEmpty output for version {version} (status {status_label}). No artifacts found."
        except Exception as error:
            raise RuntimeError(f"Output download failed for version {version}: {error}") from error

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
SENSITIVE_OR_IGNORED_NAMES = {
    ".git", ".gitignore", ".gitattributes", ".kaggle",
    ".env", ".env.local", "kaggle.json", "kaggle.toml",
    "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    ".DS_Store", "Thumbs.db", "node_modules", ".venv", "venv",
}
SENSITIVE_EXTENSIONS = {".pyc", ".pyo", ".pyd", ".pem", ".key", ".pkcs12"}


def _is_sensitive_or_ignored(path: Path) -> bool:
    name = path.name
    lower = name.lower()
    if name in SENSITIVE_OR_IGNORED_NAMES or lower in SENSITIVE_OR_IGNORED_NAMES:
        return True
    if path.suffix.lower() in SENSITIVE_EXTENSIONS:
        return True
    for part in path.parts:
        part_lower = part.lower()
        if part in SENSITIVE_OR_IGNORED_NAMES or part_lower in SENSITIVE_OR_IGNORED_NAMES:
            return True
        if part.startswith(".git"):
            return True
        if part_lower.endswith(".egg-info"):
            return True
    if any(k in lower for k in ("id_rsa", "id_ed25519", "kaggle.json", "kaggle.toml")):
        return True
    if lower.startswith(".env"):
        return True
    return False


def _collect_archive_files(sources: list[str]) -> dict[str, Path]:
    archive_map: dict[str, Path] = {}
    workspace = get_workspace_root().resolve()

    for item in sources:
        item = item.strip()
        if not item:
            continue
        # Support source:target mapping if given
        if ":" in item and not (len(item) > 1 and item[1] == ":" and item[0].isalpha()):
            src_str, tgt_str = item.split(":", 1)
        else:
            src_str, tgt_str = item, None

        raw = Path(src_str)
        source_path = (raw if raw.is_absolute() else workspace / raw).resolve()

        if not source_path.exists():
            raise FileNotFoundError(f"Dataset source path not found: {src_str} ({source_path})")

        if source_path.is_file():
            if _is_sensitive_or_ignored(source_path):
                continue
            if tgt_str:
                arc_name = tgt_str.strip().lstrip("/")
            elif source_path.is_relative_to(workspace):
                arc_name = source_path.relative_to(workspace).as_posix()
            else:
                arc_name = source_path.name

            norm = os.path.normpath(arc_name).replace("\\", "/")
            if norm.startswith("..") or norm.startswith("/") or os.path.isabs(norm):
                raise ValueError(f"Unsafe archive path '{arc_name}' for source '{source_path}'")

            if norm in archive_map and archive_map[norm] != source_path:
                raise ValueError(
                    f"Archive path collision: '{norm}' is mapped by both '{archive_map[norm]}' and '{source_path}'"
                )
            archive_map[norm] = source_path
        elif source_path.is_dir():
            target_prefix = tgt_str.strip().lstrip("/").rstrip("/") if tgt_str else None
            for file_path in sorted(source_path.rglob("*")):
                if not file_path.is_file():
                    continue
                if _is_sensitive_or_ignored(file_path):
                    continue

                if target_prefix is not None:
                    rel_to_dir = file_path.relative_to(source_path).as_posix()
                    arc_name = f"{target_prefix}/{rel_to_dir}" if target_prefix else rel_to_dir
                elif source_path.is_relative_to(workspace):
                    arc_name = file_path.relative_to(workspace).as_posix()
                else:
                    arc_name = file_path.relative_to(source_path.parent).as_posix()

                norm = os.path.normpath(arc_name).replace("\\", "/")
                if norm.startswith("..") or norm.startswith("/") or os.path.isabs(norm):
                    raise ValueError(f"Unsafe archive path '{arc_name}' for source '{file_path}'")

                if norm in archive_map and archive_map[norm] != file_path:
                    raise ValueError(
                        f"Archive path collision: '{norm}' is mapped by both '{archive_map[norm]}' and '{file_path}'"
                    )
                archive_map[norm] = file_path
        else:
            raise ValueError(f"Unsupported source path type: {source_path}")

    if not archive_map:
        raise ValueError("No valid files to package; all sources were empty or ignored.")

    return archive_map


def _create_deterministic_zip(archive_map: dict[str, Path], zip_path: Path) -> None:
    import zipfile
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    temp_zip = zip_path.with_suffix(".tmp.zip")
    if temp_zip.exists():
        temp_zip.unlink()

    sorted_items = sorted(archive_map.items(), key=lambda pair: pair[0])

    with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arc_name, file_path in sorted_items:
            zinfo = zipfile.ZipInfo(filename=arc_name, date_time=(2026, 1, 1, 0, 0, 0))
            zinfo.external_attr = 0o644 << 16
            zinfo.compress_type = zipfile.ZIP_DEFLATED
            with file_path.open("rb") as f:
                zf.writestr(zinfo, f.read())

    if zip_path.exists():
        zip_path.unlink()
    temp_zip.rename(zip_path)


@app.tool(name="build_dataset")
async def build_dataset(
    sources: list[str],
    dataset_slug: str,
    title: str | None = None,
    private: bool = True,
    upload: bool = True,
    zip_name: str | None = None,
    version_notes: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Build a Kaggle dataset ZIP archive from multiple files and/or directories and optionally upload it.

    Parameters:
    - sources: list of file and/or directory paths to package into the ZIP archive.
      Paths can be workspace-relative or absolute, and can use 'source:archive_dest' syntax.
    - dataset_slug: slug for the Kaggle dataset.
    - title: dataset title (defaults to slug titleized).
    - private: whether the dataset is private (default True).
    - upload: whether to upload to Kaggle immediately (default True).
    - zip_name: filename of the generated ZIP inside the dataset (default: '{dataset_slug}.zip').
    - version_notes: version notes when uploading a new version.
    - metadata: optional presentation metadata for dataset-metadata.json.
    """
    slug = _slugify(dataset_slug)
    if not slug:
        raise ValueError("Invalid dataset_slug")

    archive_map = _collect_archive_files(sources)

    archive_filename = zip_name or f"{slug}.zip"
    if not archive_filename.endswith(".zip"):
        archive_filename += ".zip"

    datasets_dir = get_kaggle_root() / "datasets" / slug
    datasets_dir.mkdir(parents=True, exist_ok=True)
    zip_path = datasets_dir / archive_filename

    _create_deterministic_zip(archive_map, zip_path)
    total_bytes = zip_path.stat().st_size

    manifest_lines = [f"- {arc} ({human_size(src.stat().st_size)})" for arc, src in sorted(archive_map.items())]
    summary = (
        f"Built dataset archive `{zip_path.name}` ({human_size(total_bytes)}, {len(archive_map)} files) "
        f"under `{zip_path.relative_to(get_workspace_root())}`:\n"
        + "\n".join(manifest_lines[:20])
    )
    if len(manifest_lines) > 20:
        summary += f"\n... and {len(manifest_lines) - 20} more files."

    if not upload:
        owner = _default_owner() or "owner"
        meta_path = datasets_dir / "dataset-metadata.json"
        meta_content = {
            "title": title or slug.replace("-", " ").title(),
            "id": f"{owner}/{slug}",
            "licenses": [{"name": "CC0-1.0"}],
        }
        if metadata:
            meta_content.update(metadata)
        meta_path.write_text(json.dumps(meta_content, indent=2))
        return f"{summary}\n\nDataset packaged locally; upload skipped (--no-upload)."

    upload_options = dict(
        title=title,
        dataset_slug=slug,
        private=private,
        version_notes=version_notes,
        keep_tabular=True,
        dir_mode="zip",
        metadata=metadata,
    )
    upload_res = _upload_dataset(str(zip_path), **upload_options)
    return f"{summary}\n\n{upload_res}"

@app.tool(name="push_dataset")
async def upload_dataset(
    path: str,
    title: str | None = None,
    dataset_slug: str | None = None,
    private: bool = True,
    version_notes: str | None = None,
    keep_tabular: bool = True,
    dir_mode: str = "zip",
    metadata: dict[str, Any] | None = None,
) -> str:
    """Upload or update a dataset. Please run as CLI for long uploads:

    uv run --script mcp/kaggle_mcp.py upload PATH --options '{"dataset_slug":"slug"}'

    Options are title, dataset_slug, private, version_notes, keep_tabular, dir_mode.
    MCP returns after 29 seconds with a PID and log if still running. The upload
    continues: monitor its log, do NOT restart it. Use CLI for future long uploads.
    Kaggle may automatically expand uploaded ZIPs for notebook mounts. For the Showdown
    training set, source.zip mounted as its contents, not a retained source.zip.
    Do not confuse this server behavior with the local download unzip option or
    dir_mode (upload directory packaging). Inspect actual mounted paths; do not
    promise archive preservation merely because the local upload was a ZIP.
    """
    options = dict(title=title, dataset_slug=dataset_slug, private=private,
                   version_notes=version_notes, keep_tabular=keep_tabular, dir_mode=dir_mode, metadata=metadata)
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
                    keep_tabular: bool = True, dir_mode: str = "zip",
                    metadata: dict[str, Any] | None = None) -> str:
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
    document = json.loads(meta_path.read_text())
    if document.get("id") != ref:
        raise ValueError(f"Dataset metadata id must match requested target {ref}")
    if metadata:
        allowed = {"title", "subtitle", "description", "licenses", "keywords",
                   "expectedUpdateFrequency", "userSpecifiedSources", "image", "imageUrl", "image_url"}
        if metadata.keys() - allowed:
            raise ValueError(f"Unsupported presentation fields: {sorted(metadata.keys() - allowed)}")
        image_src = metadata.get("imageUrl") or metadata.get("image_url") or metadata.get("image")
        if image_src:
            import urllib.request, mimetypes
            if image_src.startswith(("http://", "https://")):
                req = urllib.request.Request(image_src, headers={"User-Agent": "Mozilla/5.0"})
                img_data = urllib.request.urlopen(req, timeout=30).read()
                ext = mimetypes.guess_extension(urllib.request.urlopen(req).headers.get_content_type()) or ".png"
                if ext not in (".png", ".jpg", ".jpeg", ".webp"):
                    ext = ".png"
                cover_file = upload_folder / f"dataset-cover-image{ext}"
                cover_file.write_bytes(img_data)
            else:
                src_p = Path(image_src)
                ext = src_p.suffix if src_p.suffix in (".png", ".jpg", ".jpeg", ".webp") else ".png"
                cover_file = upload_folder / f"dataset-cover-image{ext}"
                cover_file.write_bytes(src_p.read_bytes())
            document["image"] = cover_file.name
        clean_meta = {k: v for k, v in metadata.items() if k not in ("image", "imageUrl", "image_url")}
        document.update(clean_meta)
    if title is not None:
        document["title"] = title
    meta_path.write_text(json.dumps(document, indent=2))

    from kaggle_transfer import DatasetApi
    from kagglesdk.datasets.types.dataset_api_service import ApiGetDatasetRequest
    from requests import HTTPError
    api = DatasetApi()
    api.authenticate()
    request = ApiGetDatasetRequest()
    request.owner_slug, request.dataset_slug = ref.split("/")
    try:
        with api.build_kaggle_client() as client:
            client.datasets.dataset_api_client.get_dataset(request)
        existed = True
    except HTTPError as error:
        if error.response is None or error.response.status_code not in (404, 403):
            raise
        existed = False
    if existed:
        result = api.dataset_create_version(str(upload_folder), version_notes or "Updated dataset",
                    convert_to_csv=not keep_tabular, dir_mode=dir_mode)
    else:
        result = api.dataset_create_new(str(upload_folder), public=not private,
                    convert_to_csv=not keep_tabular, dir_mode=dir_mode)
    if result.error:
        raise RuntimeError(result.error)
    action_msg = f"Published {'new version of' if existed else 'new'} dataset '{ref}'."

    # Save local metadata.json
    local_meta = {
        "id": ref,
        "ref": ref,
        "slug": slug,
        "title": dataset_title,
        "owner": owner,
        "private": private,
        "last_updated_utc": datetime.now(timezone.utc).isoformat(),
        "source_folder": str(upload_folder),
    }
    (datasets_dir / "metadata.json").write_text(json.dumps(local_meta, indent=2))

    return f"{action_msg}\nURL: https://www.kaggle.com/datasets/{ref}"


@app.tool(name="pull_dataset")
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



@app.tool(name="view_quota")
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
    p_wait.add_argument("--timeout", type=int, default=60, help="Wait timeout in seconds for CLI mode (default 60)")
    p_wait.add_argument("--poll-interval", type=int, default=5)
    p_wait.add_argument("--until", choices=("complete", "log"), default="complete")
    p_wait.add_argument("--text")
    p_wait.add_argument("--version", type=int)
    p_wait.add_argument("--versions", nargs="+", type=int)

    # status
    p_status = subparsers.add_parser("status", help="Check notebook status")
    p_status.add_argument("notebook", help="Notebook name or directory")
    p_status.add_argument("--logs", action="store_true", help="Fetch logs")
    p_status.add_argument("--tail", type=int, default=10, help="Number of log lines to show")
    p_status.add_argument("--grep", help="Regex pattern to filter log lines before tail")
    p_status.add_argument("--context", type=int, default=3, help="Lines before and after each grep match")
    p_status.add_argument("--log-timeout", type=int, default=60, help="Log streaming wait timeout in seconds for CLI mode (default 60)")
    p_status.add_argument("--version", type=int)

    p_cancel = subparsers.add_parser("cancel", help="Cancel a verified active notebook run")
    p_cancel.add_argument("notebook")
    p_cancel.add_argument("--version", type=int)
    p_cancel.add_argument("--dry-run", action="store_true")

    p_edit = subparsers.add_parser("edit-notebook", help="Publish title or Markdown without execution")
    p_edit.add_argument("notebook")
    p_edit.add_argument("--title")
    p_edit.add_argument("--markdown-path")

    # output
    p_output = subparsers.add_parser("output", help="Download notebook outputs")
    p_output.add_argument("notebook", help="Notebook name or directory")
    p_output.add_argument("--run", type=int, help="Specific run number")
    p_output.add_argument("--pattern", help="Selective glob pattern to filter output files (e.g. 'terminal-credit-output/*')")
    p_output.add_argument("--timeout", type=int, default=1800, help="Download timeout in seconds (default 1800)")

    # quota
    subparsers.add_parser("quota", help="Check accelerator quota")
    edit_dataset = subparsers.add_parser("edit-dataset", help="Edit dataset metadata and visibility without uploading files")
    edit_dataset.add_argument("dataset")
    edit_dataset.add_argument("--changes", required=True, help='JSON fields, including isPrivate true or false')
    view_dataset = subparsers.add_parser("view-dataset", help="Read dataset metadata and visibility")
    view_dataset.add_argument("dataset")
    upload = subparsers.add_parser("upload", help="Upload dataset without the MCP call timeout")
    upload.add_argument("path")
    upload.add_argument("--options", default="{}", help="JSON upload options")
    download = subparsers.add_parser("download", help="Download dataset without the MCP call timeout")
    download.add_argument("dataset")
    download.add_argument("--options", default="{}", help="JSON download options")
    # build-dataset
    p_build = subparsers.add_parser("build-dataset", help="Build and optionally upload dataset")
    p_build.add_argument("sources", nargs="+", help="Files and/or directories to include in dataset archive")
    p_build.add_argument("--slug", required=True, help="Dataset slug")
    p_build.add_argument("--title", help="Dataset title")
    p_build.add_argument("--public", action="store_true", help="Make dataset public (default is private)")
    p_edit_model = subparsers.add_parser("edit-model", help="Edit model metadata and publish model card updates")
    p_edit_model.add_argument("path", help="Folder containing model-metadata.json or model directory")
    p_edit_model.add_argument("--variation", action="store_true", help="Update variation instance metadata instead of parent model")
    p_edit_model.add_argument("--changes", help="JSON string of updates (title, subtitle, description, isPrivate)")
    p_build.add_argument("--no-upload", action="store_true", help="Build ZIP staging locally without uploading")
    p_build.add_argument("--zip-name", help="Archive file name inside dataset (default: <slug>.zip)")
    p_build.add_argument("--notes", help="Version notes")
    p_build.add_argument("--metadata", help="JSON metadata string")
    for operation in ("push-model", "pull-model"):
        transfer = subparsers.add_parser(operation, help="Transfer model weights without MCP timeout")
        transfer.add_argument("source")
        transfer.add_argument("--options", default="{}")

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
            _print_result(asyncio.run(get_quota()))
        elif cmd == "edit-model":
            changes = json.loads(args.changes) if args.changes else None
            _print_result(asyncio.run(edit_model(args.path, variation=args.variation, changes=changes)))
        elif cmd == "edit-dataset":
            _print_result(asyncio.run(update_dataset_metadata(args.dataset, json.loads(args.changes))))
            _print_result(asyncio.run(read_metadata(args.dataset, "datasets")))
        elif cmd == "status":
            fetch_logs = args.logs or bool(args.grep)
            _print_result(asyncio.run(view_status(args.notebook, tail=args.tail, fetch_logs=fetch_logs, grep=args.grep, context=args.context, log_timeout=args.log_timeout, version=args.version)))
        elif cmd == "cancel":
            _print_result(asyncio.run(cancel_run(args.notebook, version=args.version, dry_run=args.dry_run)))
        elif cmd == "edit-notebook":
            _print_result(asyncio.run(update_notebook_presentation(args.notebook, title=args.title, markdown_path=args.markdown_path)))
        elif cmd == "push":
            _print_result(asyncio.run(push_notebook(args.notebook, accelerator=args.accelerator)))
        elif cmd == "save":
            _print_result(asyncio.run(save_notebook(args.notebook)))
        elif cmd == "wait":
            _print_result(asyncio.run(wait(args.notebook, until=args.until, text=args.text, timeout=args.timeout, poll_interval=args.poll_interval, version=args.version, versions=args.versions)))
        elif cmd == "output":
            _print_result(asyncio.run(fetch_output(args.notebook, run_number=args.run, pattern=args.pattern, timeout=args.timeout)))
        elif cmd == "upload":
            _print_result(_upload_dataset(args.path, **json.loads(args.options)))
        elif cmd == "download":
            _print_result(_download_dataset(args.dataset, **json.loads(args.options)))
        elif cmd == "build-dataset":
            meta = json.loads(args.metadata) if args.metadata else None
            _print_result(asyncio.run(build_dataset(
                sources=args.sources,
                dataset_slug=args.slug,
                title=args.title,
                private=not args.public,
                upload=not args.no_upload,
                zip_name=args.zip_name,
                version_notes=args.notes,
                metadata=meta,
            )))
        elif cmd in ("push-model", "pull-model"):
            _print_result(_model_transfer(cmd, args.source, json.loads(args.options)))
        return

    # MCP server mode
    app.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
