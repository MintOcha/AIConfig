#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["fastmcp==3.4.5", "google-colab-cli==0.7.2"]
# [tool.uv]
# override-dependencies = ["jupyter-kernel-client==0.9.0"]
# ///
"""Manage Colab projects through Google's CLI. Login: colab --auth oauth2 sessions.

For interactive work, create/reuse a named session, send short stdin cells with
exec, and launch long jobs separately so their kernel stays responsive.

A managed run is asynchronous. Runtimes are not kept alive beyond provider limits;
training code must checkpoint periodically under /content for explicit retrieval.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import UUID, uuid4

from fastmcp import FastMCP
from colab_cli.auth import get_credentials
from colab_cli.client import Client, Prod
from colab_cli.state import SessionState, StateStore

app = FastMCP("colab", instructions=(
    "Authenticate with official `colab --auth oauth2 sessions`. For interactive work, "
    "use session_list, session_new, session_status, session_exec, and session_stop with explicit names. "
    "Use session_launch for long commands and session_job_status/session_job_logs to inspect them "
    "without tying up the kernel. For managed batch runs use save, stage, push, status, logs, wait, "
    "pull, release, and cancel. One managed run; TPU is verified by XLA forward/backward. "
    "Colab quota and termination are provider-controlled. Checkpoint under /content and retrieve "
    "checkpoints before stopping the VM."
))
WORKSPACE = Path(os.environ.get("COLAB_MCP_WORKSPACE", Path.cwd())).resolve()
ROOT = Path(os.environ.get("COLAB_MCP_ROOT", WORKSPACE / "colab")).resolve()
STATE = Path.home() / ".config" / "colab-mcp"
SESSION = os.environ.get("COLAB_MCP_SESSION", "managed-train")
CLI = [str(Path(sys.executable).with_name("colab")), "--auth", "oauth2"]
CHUNK = 8 * 1024 * 1024
REMOTE_ROOT = "/content/colab-managed"
LIVE_ROOT = "/content/colab-live"
ACCELERATORS = ("CPU", "T4", "L4", "G4", "A100", "H100", "V5E1", "V6E1")


def _norm_accel(value: str) -> str:
    upper = value.upper()
    return "CPU" if upper == "NONE" else upper


def _session_arg(session: str) -> str:
    if not session.strip() or any(char in session for char in ("/", "\\", "\0")):
        raise ValueError("Provide an explicit Colab session name without path separators")
    return f"--session={session}"


def _session(session: str | None = None) -> dict:
    """Refresh a live assignment's short-lived proxy token before kernel calls."""
    store = StateStore()
    name = SESSION if session is None else session
    saved = store.get(name)
    if session is not None and saved is None:
        raise RuntimeError(f"Unknown Colab session {session!r}; list sessions or create it first")
    client = Client(Prod(), get_credentials())
    assignments = client.list_assignments()
    if saved:
        matches = [assignment for assignment in assignments if assignment.endpoint == saved.endpoint]
    else:
        matches = [assignment for assignment in assignments if assignment.accelerator.value != "NONE"]
        if len(assignments) == 1 and not matches:
            matches = assignments
        if len(assignments) != 1 or len(matches) != 1:
            raise RuntimeError("Cannot adopt ambiguous or missing Colab assignment; inspect `colab sessions`")
    if len(matches) != 1:
        raise RuntimeError(f"Colab session {name!r} is not active on the provider")
    assignment = matches[0]
    if saved is None:
        saved = SessionState(name=name, token=assignment.runtime_proxy_info.token,
                             url=assignment.runtime_proxy_info.url, endpoint=assignment.endpoint,
                             variant=assignment.variant.name, accelerator=assignment.accelerator.value,
                             machine_shape=assignment.machine_shape.name)
    else:
        saved.token = assignment.runtime_proxy_info.token
        saved.url = assignment.runtime_proxy_info.url
    store.add(saved)
    return {"endpoint": assignment.endpoint, "accelerator": _norm_accel(assignment.accelerator.value),
            "variant": assignment.variant.name}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w") as stream:
        os.chmod(temp, 0o600)
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _project(name: str) -> Path:
    if not name or Path(name).name != name or name in (".", "..") or "/" in name or "\\" in name:
        raise ValueError("Project must be a single folder name")
    folder = (ROOT / name).resolve()
    if not folder.is_relative_to(ROOT) or not folder.is_dir():
        raise ValueError("Project folder missing")
    return folder


def _safe_remote(path: str) -> str:
    from pathlib import PurePosixPath
    remote = PurePosixPath(path)
    if not path.startswith("/content/") or ".." in remote.parts or str(remote) != path:
        raise ValueError("Remote path must be an absolute path under /content without traversal")
    return path


def _source(folder: Path) -> tuple[Path, dict]:
    meta = _load(folder / "colab-metadata.json")
    source = next((folder / name for name in ("train.py", "notebook.ipynb") if (folder / name).is_file()), None)
    if not source or not source.stat().st_size:
        raise ValueError("Expected nonempty train.py or notebook.ipynb")
    if meta.get("accelerator") not in ACCELERATORS:
        raise ValueError("Invalid accelerator")
    seconds = meta.get("max_seconds", 36000)
    if type(seconds) is not int or not 1 <= seconds <= 43200:
        raise ValueError("max_seconds must be integer 1..43200")
    inputs = meta.get("inputs", [])
    if not isinstance(inputs, list) or any(not isinstance(item, str) for item in inputs):
        raise ValueError("inputs must list local workspace-relative paths")
    for item in inputs:
        _local_input(item)
    return source, meta


def _local_input(path: str) -> Path:
    if not path or Path(path).is_absolute() or ".." in Path(path).parts:
        raise ValueError("Input must be workspace-relative without traversal")
    local = (WORKSPACE / path).resolve(strict=True)
    if not local.is_relative_to(WORKSPACE):
        raise ValueError("Input escapes workspace")
    return local


def _hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _cli(args: list[str], *, timeout: int = 120, input: str | None = None,
         session: str | None = None) -> str:
    if args[0] in ("exec", "upload", "download"):
        _session(session)
    result = subprocess.run(CLI + args, input=input, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"Colab command {args[0]} failed (exit {result.returncode}): {result.stdout[-1000:]}")
    return result.stdout


def _exec(code: str, timeout: int = 30, retries: int = 1, *, session: str | None = None) -> str:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            target = f"--session={SESSION}" if session is None else _session_arg(session)
            return _cli(["exec", target, "--timeout", str(timeout)], input=code,
                        timeout=timeout + 15, session=session)
        except (subprocess.TimeoutExpired, RuntimeError) as exc:
            last_exc = exc
            is_timeout = isinstance(exc, subprocess.TimeoutExpired) or "timed out" in str(exc).lower()
            if attempt >= retries or not is_timeout:
                raise
    raise last_exc  # pragma: no cover


def _remote_json(code: str, timeout: int = 30, retries: int = 1, *, session: str | None = None) -> dict:
    output = _exec(code, timeout=timeout, retries=retries, session=session)
    for line in reversed(output.splitlines()):
        if line.startswith("COLAB_JSON:"):
            return json.loads(line.removeprefix("COLAB_JSON:"))
    raise RuntimeError("Remote command did not return expected JSON: " + output[-500:])


_JOB_RUNNER = """\
import json, os, subprocess, sys, time

status_path, cwd, *command = sys.argv[1:]
def write_state(state):
    temp = status_path + '.tmp'
    with open(temp, 'w') as stream:
        json.dump(state, stream)
    os.replace(temp, status_path)

write_state({'state': 'running', 'pid': os.getpid(), 'started': time.time()})
try:
    result = subprocess.run(command, cwd=cwd)
except (OSError, ValueError) as exc:
    print(f'{type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
    final = {'state': 'error', 'error': str(exc)}
else:
    final = {'state': 'complete' if result.returncode == 0 else 'error',
             'exit_code': result.returncode}
write_state(final | {'finished': time.time()})
"""


def _cell(code: str, **inputs) -> str:
    """Run an internal cell without overwriting the caller's kernel variables."""
    return f"exec({code!r}, {inputs!r})"


def _launch(root: str, command: list[str], *, cwd: str, log_name: str,
            kernel_python: bool = False, session: str | None = None) -> dict:
    code = _cell("""\
import json, os, subprocess, sys, time
from pathlib import Path

root = Path(root)
root.mkdir(parents=True, exist_ok=True)
status_path = root / 'result.json'
log_path = root / log_name
args = [sys.executable, *command] if kernel_python else command
env = dict(os.environ, PYTHONUNBUFFERED='1')
with log_path.open('wb') as out:
    proc = subprocess.Popen([sys.executable, '-c', runner, str(status_path), cwd, *args],
                            stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                            start_new_session=True, env=env)
deadline = time.monotonic() + 5.0
while not status_path.is_file():
    if proc.poll() is not None or time.monotonic() >= deadline:
        raise RuntimeError(f'Job did not report startup (pid {proc.pid}); inspect {log_path}')
    time.sleep(0.05)
status = json.loads(status_path.read_text())
status.update(pid=proc.pid, status_path=str(status_path), log_path=str(log_path))
print('COLAB_JSON:' + json.dumps(status), flush=True)
""", root=root, command=command, cwd=cwd, log_name=log_name, runner=_JOB_RUNNER,
        kernel_python=kernel_python)
    return _remote_json(code, retries=0, session=session)


def _job_dir(job_id: str) -> str:
    if UUID(job_id).hex != job_id:
        raise ValueError("Expected the job_id returned by launch")
    return f"{LIVE_ROOT}/{job_id}"


def _new_args(session: str, accelerator: str, high_mem: bool = False) -> list[str]:
    args = ["new", _session_arg(session)]
    if accelerator.startswith("V"):
        args += ["--tpu", accelerator.lower()]
    elif accelerator != "CPU":
        args += ["--gpu", accelerator]
    if high_mem:
        args.append("--high-mem")
    return args


@app.tool()
def session_list() -> str:
    """List active Colab runtimes and their official CLI session names."""
    return _cli(["sessions"])


@app.tool()
def session_new(session: str, accelerator: str = "CPU", high_mem: bool = False) -> str:
    """Create an explicitly named CPU, GPU, or TPU runtime for interactive work."""
    accelerator = accelerator.upper()
    if accelerator not in ACCELERATORS:
        raise ValueError(f"Accelerator must be one of {', '.join(ACCELERATORS)}")
    if StateStore().get(session) is not None:
        raise ValueError(f"Colab session {session!r} is already named locally; list sessions before creating it")
    return _cli(_new_args(session, accelerator, high_mem), timeout=240)


@app.tool()
def session_status(session: str) -> str:
    """Show provider hardware and CLI kernel activity for a named session."""
    return _cli(["status", _session_arg(session)])


@app.tool()
def session_stop(session: str) -> str:
    """Stop the named session, losing its processes and uncollected files."""
    return _cli(["stop", _session_arg(session)])


@app.tool()
def session_exec(session: str, code: str, timeout: int = 30) -> str:
    """Run a short Python cell in the named session's existing kernel; use launch for long commands."""
    if not code.strip() or timeout < 1:
        raise ValueError("Provide nonempty Python code and a positive timeout in seconds")
    return _exec(code, timeout=timeout, retries=0, session=session)


@app.tool()
def session_launch(session: str, command: list[str], cwd: str = "/content") -> dict:
    """Detach an argv command on the VM and return its job ID, supervisor PID, and paths."""
    if not command or not command[0]:
        raise ValueError("Provide a program and any arguments as an argv list")
    job_id = uuid4().hex
    try:
        result = _launch(_job_dir(job_id), command, cwd=cwd, log_name="output.log", session=session)
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Could not confirm launch of Colab job {job_id}: {exc}") from exc
    return result | {"session": session, "job_id": job_id}


def _job_status(root: str, *, session: str | None = None) -> dict:
    code = _cell("""\
import json
from pathlib import Path

path = Path(root) / 'result.json'
state = json.loads(path.read_text())
if state['state'] == 'running':
    try:
        stat = Path(f"/proc/{state['pid']}/stat").read_text()
        alive = stat.rsplit(')', 1)[1].split()[0] != 'Z'
    except FileNotFoundError:
        alive = False
    if not alive:
        state = json.loads(path.read_text())
        if state['state'] == 'running':
            state.update(state='error', error='Remote job exited without recording a result')
print('COLAB_JSON:' + json.dumps(state), flush=True)
""", root=root)
    return _remote_json(code, retries=int(session is None), session=session)


@app.tool()
def session_job_status(session: str, job_id: str) -> dict:
    """Read the detached job state or exit code on its named session."""
    return _job_status(_job_dir(job_id), session=session) | {"session": session, "job_id": job_id}


@app.tool()
def session_job_logs(session: str, job_id: str, lines: int = 40) -> str:
    """Read recent stdout/stderr from a detached job (at most 500 lines and 64 KiB)."""
    code = _cell("""\
import json
from pathlib import Path

with (Path(root) / 'output.log').open('rb') as stream:
    stream.seek(0, 2)
    stream.seek(max(0, stream.tell() - 65536))
    tail = stream.read(65536).decode(errors='replace')
print('COLAB_JSON:' + json.dumps({'output': ''.join(tail.splitlines(keepends=True)[-lines:])}), flush=True)
""", root=_job_dir(job_id), lines=min(max(lines, 1), 500))
    return _remote_json(code, retries=0, session=session)["output"]


def _remote_file(path: str) -> dict:
    path = _safe_remote(path)
    return _remote_json("import os,json,hashlib; p=" + repr(path) + "; print('COLAB_JSON:'+json.dumps({'exists':os.path.isfile(p),'size':os.path.getsize(p) if os.path.isfile(p) else None,'sha256':hashlib.file_digest(open(p,'rb'),'sha256').hexdigest() if os.path.isfile(p) else None}),flush=True)")


def _transfer(local: Path, remote: str) -> dict:
    """Bounded uploads with resumable files, verification, and atomic assembly."""
    remote = _safe_remote(remote)
    if not local.is_file():
        raise ValueError("Only regular files can be transferred")
    digest = _hash(local)
    size = local.stat().st_size
    parent = str(Path(remote).parent)
    existing = _remote_json(
        "import os,json,hashlib; p=" + repr(remote) + "; os.makedirs(" + repr(parent) + ",exist_ok=True); "
        "print('COLAB_JSON:'+json.dumps({'exists':os.path.isfile(p),'size':os.path.getsize(p) if os.path.isfile(p) else None,"
        "'sha256':hashlib.file_digest(open(p,'rb'),'sha256').hexdigest() if os.path.isfile(p) else None}),flush=True)"
    )
    if existing["size"] == size and existing["sha256"] == digest:
        return {"path": remote, "size": size, "sha256": digest, "resumed": True}
    if size <= CHUNK:
        staging = f"{remote}.uploading"
        _cli(["upload", "-s", SESSION, str(local), staging], timeout=180)
        code = (
            "import os,hashlib,json; src=" + repr(staging) + "; dst=" + repr(remote) + "; "
            "ok=os.path.isfile(src) and hashlib.file_digest(open(src,'rb'),'sha256').hexdigest()==" + repr(digest) + "; "
            "os.replace(src,dst) if ok else None; "
            "print('COLAB_JSON:'+json.dumps({'verified':ok}),flush=True)"
        )
        if not _remote_json(code)["verified"]:
            raise RuntimeError("Remote file failed SHA256 verification")
        return {"path": remote, "size": size, "sha256": digest, "resumed": False}
    parts = []
    with local.open("rb") as stream:
        number = 0
        while chunk := stream.read(CHUNK):
            part = f"{remote}.part.{number:05d}"
            part_hash = hashlib.sha256(chunk).hexdigest()
            if _remote_file(part)["sha256"] != part_hash:
                import tempfile
                with tempfile.NamedTemporaryFile() as temp:
                    temp.write(chunk)
                    temp.flush()
                    _cli(["upload", "-s", SESSION, temp.name, part], timeout=180)
                if _remote_file(part)["sha256"] != part_hash:
                    raise RuntimeError(f"Uploaded part {number} failed integrity check")
            parts.append(part)
            number += 1
    code = "import os,hashlib,json; dst=" + repr(remote) + "; parts=" + repr(parts) + "; tmp=dst+'.assembling'; os.makedirs(os.path.dirname(dst),exist_ok=True); o=open(tmp,'wb'); [o.write(open(p,'rb').read()) for p in parts]; o.close(); ok=hashlib.file_digest(open(tmp,'rb'),'sha256').hexdigest()==" + repr(digest) + "; os.replace(tmp,dst) if ok else None; [os.unlink(p) for p in parts] if ok else None; print('COLAB_JSON:'+json.dumps({'verified':ok}),flush=True)"
    if not _remote_json(code)["verified"]:
        raise RuntimeError("Remote file failed SHA256 verification")
    return {"path": remote, "size": size, "sha256": digest, "resumed": False}


def _active() -> dict:
    return _load(STATE / "active.json")


def _latest(folder: Path) -> Path | None:
    runs = sorted((folder / "runs").glob("*/status.json")) if (folder / "runs").exists() else []
    return runs[-1].parent if runs else None


@app.tool()
def save(project: str) -> dict:
    """Validate local .py/.ipynb and accelerator metadata without starting execution."""
    folder = _project(project)
    source, meta = _source(folder)
    info = {"sha256": _hash(source), "accelerator": meta["accelerator"], "inputs": meta.get("inputs", [])}
    _save(folder / "source.json", info)
    return {"project": project, **info}


@app.tool()
def stage(project: str, input_path: str) -> dict:
    """Transfer one workspace file/directory in bounded chunks; resume verified files."""
    local = _local_input(input_path)
    files = sorted(path for path in local.rglob("*") if path.is_file()) if local.is_dir() else [local]
    if any(not path.resolve().is_relative_to(WORKSPACE) for path in files):
        raise ValueError("Input directory contains file escaping workspace")
    digest = hashlib.sha256()
    total = 0
    for index, path in enumerate(files, 1):
        item = _transfer(path, f"{REMOTE_ROOT}/{path.relative_to(WORKSPACE).as_posix()}")
        digest.update(item["sha256"].encode())
        total += item["size"]
        print(json.dumps({"file": index, "of": len(files), "path": path.relative_to(WORKSPACE).as_posix(), "bytes": total, "resumed": item["resumed"]}), file=sys.stderr, flush=True)
    return {"files": len(files), "bytes": total, "sha256": digest.hexdigest()}


def _probe(accelerator: str) -> str:
    if accelerator.startswith("V"):
        code = "import torch,torch_xla.core.xla_model as xm; d=xm.xla_device(); t=torch.randn(32,32,device=d,requires_grad=True); (t@t).square().mean().backward(); xm.mark_step(); print('TPU_PROBE_OK', d,flush=True)"
        marker = "TPU_PROBE_OK"
    elif accelerator == "CPU":
        code = "import torch; print('CPU_PROBE_OK',flush=True)"
        marker = "CPU_PROBE_OK"
    else:
        code = "import torch; assert torch.cuda.is_available(); t=torch.randn(32,32,device='cuda',requires_grad=True); (t@t).square().mean().backward(); print('GPU_PROBE_OK',torch.cuda.get_device_name(),flush=True)"
        marker = "GPU_PROBE_OK"
    output = _exec(code, timeout=180)
    if marker not in output:
        raise RuntimeError("Accelerator forward/backward not verified: " + output[-500:])
    return output.strip()


def _worker(project: str, run: Path, reuse: bool) -> None:
    folder = _project(project)
    source, meta = _source(folder)
    accelerator = _norm_accel(meta["accelerator"])
    step = "init"
    with (run / "worker.log").open("a", buffering=1) as log:
        def mark(name: str, detail: str = "") -> None:
            nonlocal step
            step = name
            log.write(f"[{time.strftime('%H:%M:%S')}] step={name}" + (f" {detail}" if detail else "") + "\n")

        try:
            session = None
            if reuse:
                mark("adopt_session")
                try:
                    session = _session()
                except RuntimeError:
                    if Client(Prod(), get_credentials()).list_assignments():
                        raise
            if session is None:
                mark("provision", f"accelerator={accelerator}")
                args = _new_args(SESSION, accelerator)
                try:
                    log.write(_cli(args, timeout=240))
                except RuntimeError as exc:
                    if "ReadTimeout" not in str(exc) and "timed out" not in str(exc).lower():
                        raise
                    mark("provision_adopt", "colab new timed out; checking provider for live assignment")
                    deadline = time.monotonic() + 30.0
                    while time.monotonic() < deadline:
                        if Client(Prod(), get_credentials()).list_assignments():
                            break
                        time.sleep(3)
                    else:
                        raise
                session = _session()
            if session["accelerator"] != accelerator:
                raise RuntimeError(f"Live Colab accelerator ({session['accelerator']}) does not match project metadata ({accelerator})")
            mark("probe", f"endpoint={session['endpoint']} accelerator={accelerator}")
            probe = _probe(accelerator)
            log.write(probe + "\n")
            _save(run / "status.json", {"state": "staging", "accelerator": accelerator, "probe": probe, "started": time.time()})
            for item in meta.get("inputs", []):
                mark("stage", item)
                result = stage(project, item)
                log.write(f"Staged {item}: {result['files']} files, {result['bytes']} bytes\n")
            remote = f"{REMOTE_ROOT}/{project}/{run.name}"
            remote_source = f"{remote}/{source.name}"
            mark("upload_source", remote_source)
            _transfer(source, remote_source)
            mark("launch", remote)
            # Launch a detached subprocess; the wrapper owns result.json transitions atomically.
            result = _launch(remote, [remote_source], cwd="/content", log_name="train.log",
                             kernel_python=True)
            mark("running", f"pid={result['pid']}")
            _save(run / "status.json", {"state": "running", "accelerator": accelerator,
                                         "remote": remote, "pid": result["pid"], "started": time.time()})
        except Exception as exc:
            message = f"[{step}] {type(exc).__name__}: {str(exc)[-600:]}"
            log.write(message + "\n")
            _save(run / "status.json", {"state": "error", "step": step, "error": message, "finished": time.time()})


@app.tool()
def push(project: str, reuse: bool = True) -> dict:
    """Start an asynchronous run, optionally attaching to an existing named Colab VM."""
    folder = _project(project)
    source, meta = _source(folder)
    if (STATE / "active.json").exists():
        raise RuntimeError("Managed Colab run already active; retrieve checkpoints and cancel first")
    if not (Path.home() / ".config/colab-cli/token.json").is_file():
        raise RuntimeError("Authenticate first via official `colab --auth oauth2 sessions`")
    run = folder / "runs" / time.strftime("%Y%m%d-%H%M%S")
    run.mkdir(parents=True, exist_ok=False)
    _save(run / "status.json", {"state": "starting", "accelerator": meta["accelerator"], "source_sha256": _hash(source)})
    _save(STATE / "active.json", {"project": project, "run": str(run)})
    with (run / "worker.log").open("a") as log:
        subprocess.Popen([sys.executable, __file__, "worker", project, str(run), str(int(reuse))],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    return {"run": str(run), "state": "starting", "accelerator": meta["accelerator"]}


@app.tool()
def status(project: str) -> dict:
    """Read local status plus remote exit status when a managed process was started."""
    run = _latest(_project(project))
    if not run:
        return {"state": "not_started"}
    state = _load(run / "status.json")
    if state["state"] == "running":
        try:
            current = _job_status(state["remote"])
            if current["state"] != "running":
                state = {**state, **current}
                _save(run / "status.json", state)
        except Exception as exc:
            state["remote_status_unavailable"] = f"{type(exc).__name__}: {str(exc)[-250:]}"
    return state | {"run": str(run)}


@app.tool()
def logs(project: str, lines: int = 40) -> str:
    """Read recent persisted remote training output or staging diagnostics."""
    run = _latest(_project(project))
    if not run:
        return "No run"
    state = _load(run / "status.json")
    if state["state"] in ("running", "complete", "error") and "remote" in state:
        remote = state["remote"]
        code = "import pathlib; p=pathlib.Path(" + repr(remote) + ")/'train.log'; print(''.join(p.read_text(errors='replace').splitlines(keepends=True)[-" + str(min(max(lines, 1), 500)) + ":]) if p.exists() else 'No output yet',flush=True)"
        try:
            return _exec(code)
        except Exception:
            pass
    path = run / "worker.log"
    return "\n".join(path.read_text().splitlines()[-min(max(lines, 1), 500):]) if path.exists() else "No output yet"


@app.tool()
def wait(project: str, timeout: int = 300) -> dict:
    """Wait for completion; timeout or inaccessible remote never means success."""
    deadline = time.monotonic() + min(max(timeout, 1), 1800)
    while time.monotonic() < deadline:
        current = status(project)
        if current["state"] in ("complete", "error", "cancelled", "not_started"):
            return current
        time.sleep(10)
    return status(project) | {"wait_timeout": True}


@app.tool()
def pull(project: str, remote_path: str, local_name: str) -> dict:
    """Retrieve a remote checkpoint in bounded chunks and verify SHA256."""
    folder = _project(project)
    active = _active()
    if active["project"] != project:
        raise ValueError("Active run belongs to another project")
    remote_path = _safe_remote(remote_path)
    if local_name in ("", ".", "..") or Path(local_name).name != local_name:
        raise ValueError("local_name must be a basename")
    descriptor = _remote_file(remote_path)
    if not descriptor["exists"]:
        raise FileNotFoundError(remote_path)
    target = Path(active["run"]) / local_name
    pending = target.with_name(target.name + ".partial")
    with pending.open("wb") as output:
        for offset in range(0, descriptor["size"], CHUNK):
            fragment = f"{remote_path}.download.{offset // CHUNK:05d}"
            _remote_json("p=" + repr(remote_path) + "; q=" + repr(fragment) + "; import json; f=open(p,'rb'); f.seek(" + str(offset) + "); b=f.read(" + str(CHUNK) + "); f.close(); open(q,'wb').write(b); print('COLAB_JSON:'+json.dumps({'size':len(b)}),flush=True)")
            import tempfile
            with tempfile.NamedTemporaryFile() as temp:
                _cli(["download", "-s", SESSION, fragment, temp.name], timeout=180)
                temp.seek(0)
                while block := temp.read(1024 * 1024):
                    output.write(block)
            _exec("import os; os.unlink(" + repr(fragment) + ")")
    if _hash(pending) != descriptor["sha256"]:
        raise RuntimeError("Downloaded checkpoint SHA256 mismatch; partial preserved")
    pending.replace(target)
    return {"path": str(target), "size": target.stat().st_size, "sha256": descriptor["sha256"]}


@app.tool()
def release(project: str) -> dict:
    """Release finished job ownership without stopping its Colab VM."""
    _project(project)
    active = _active()
    if active["project"] != project:
        raise ValueError("Managed run belongs to another project")
    current = status(project)
    if current["state"] not in ("complete", "error", "cancelled"):
        raise RuntimeError("Running job must finish or be cancelled first")
    (STATE / "active.json").unlink()
    return {"released": True, "session_still_running": True}


@app.tool()
def cancel(project: str) -> dict:
    """Stop Colab VM, losing any unpulled checkpoints."""
    _project(project)
    active = _active()
    if active["project"] != project:
        raise ValueError("Active run belongs to another project")
    stopped_remote = True
    try:
        _cli(["stop", "-s", SESSION], timeout=120)
    except RuntimeError:
        stopped_remote = False
    (STATE / "active.json").unlink(missing_ok=True)
    run = Path(active["run"])
    previous = _load(run / "status.json")
    if previous["state"] not in ("complete", "error"):
        _save(run / "status.json", {**previous, "state": "cancelled", "finished": time.time()})
    return {"released": True, "stopped_remote": stopped_remote, "run": str(run)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(metavar="COMMAND")
    parser.set_defaults(handler=app.run)

    def command(name, handler, description):
        sub = commands.add_parser(name, help=description, description=description)
        sub.set_defaults(handler=handler)
        return sub

    command("serve", app.run, "Start the MCP server (default)")
    worker = command("worker", _worker, "Run the managed batch worker")
    worker.add_argument("project")
    worker.add_argument("run", type=Path)
    worker.add_argument("reuse", type=lambda value: bool(int(value)))
    for handler in (save, stage, push, status, logs, wait, pull, release, cancel):
        sub = command(handler.__name__, handler, handler.__doc__)
        sub.add_argument("project")
        if handler is stage:
            sub.add_argument("input_path")
        elif handler is push:
            sub.add_argument("reuse", nargs="?", default=True,
                             type=lambda value: value.lower() not in ("false", "0"))
        elif handler is logs:
            sub.add_argument("lines", nargs="?", type=int, default=40)
        elif handler is wait:
            sub.add_argument("timeout", nargs="?", type=int, default=300)
        elif handler is pull:
            sub.add_argument("remote_path")
            sub.add_argument("local_name")

    command("sessions", session_list, "List active Colab sessions")
    for name, handler in (
        ("new", session_new), ("session-status", session_status), ("stop", session_stop),
        ("exec", session_exec), ("launch", session_launch), ("job-status", session_job_status),
        ("job-logs", session_job_logs),
    ):
        description = ("Read a short Python cell from stdin and run it in a named kernel"
                       if handler is session_exec else handler.__doc__)
        sub = command(name, handler, description)
        sub.add_argument("-s", "--session", required=True, metavar="NAME")
        if handler is session_new:
            sub.add_argument("--accelerator", type=str.upper, choices=ACCELERATORS, default="CPU")
            sub.add_argument("--high-mem", action="store_true")
        elif handler is session_exec:
            sub.add_argument("--timeout", type=int, default=30,
                             help="Client wait in seconds; timing out does not stop the cell")
        elif handler is session_launch:
            sub.add_argument("--cwd", default="/content", help="Working directory on the Colab VM")
            sub.add_argument("command", nargs=argparse.REMAINDER, metavar="PROGRAM [ARG ...]")
        elif handler in (session_job_status, session_job_logs):
            sub.add_argument("job_id")
            if handler is session_job_logs:
                sub.add_argument("--lines", type=int, default=40)

    values = vars(parser.parse_args())
    handler = values.pop("handler")
    if handler is session_exec:
        if sys.stdin.isatty():
            parser.error("exec reads a Python cell from stdin; pipe or redirect it")
        values["code"] = sys.stdin.read()
    elif handler is session_launch:
        if values["command"][:1] == ["--"]:
            values["command"] = values["command"][1:]
        if not values["command"]:
            parser.error("launch requires a PROGRAM [ARG ...]")
    output = handler(**values)
    if isinstance(output, str):
        print(output, end="" if output.endswith("\n") else "\n")
    elif output is not None:
        print(json.dumps(output))


if __name__ == "__main__":
    main()
