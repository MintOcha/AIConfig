#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["fastmcp==3.4.5", "google-colab-cli==0.7.2"]
# ///
# [tool.uv]
# override-dependencies = ["jupyter-kernel-client==0.9.0"]
"""Manage Colab projects through Google's CLI. Login: colab --auth oauth2 sessions.

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

from fastmcp import FastMCP
from colab_cli.auth import get_credentials
from colab_cli.client import Client, Prod
from colab_cli.state import SessionState, StateStore

app = FastMCP("colab", instructions="Authenticate interactively with official `colab --auth oauth2 sessions`; use save, stage, push, status, logs, wait, pull, cancel. One managed run; TPU requested with --tpu v5e1/v6e1, verified by XLA forward/backward. Colab quota and runtime termination are provider-controlled. Run code must checkpoint under /content; retrieve it before releasing the VM.")
WORKSPACE = Path(os.environ.get("COLAB_MCP_WORKSPACE", Path.cwd())).resolve()
ROOT = Path(os.environ.get("COLAB_MCP_ROOT", WORKSPACE / "colab")).resolve()
STATE = Path.home() / ".config" / "colab-mcp"
SESSION = os.environ.get("COLAB_MCP_SESSION", "managed-train")
CLI = ["uvx", "--from", "google-colab-cli==0.7.2", "--overrides", str(Path(__file__).with_name("colab-kernel-overrides.txt")), "colab", "--auth", "oauth2"]
CHUNK = 8 * 1024 * 1024
REMOTE_ROOT = "/content/colab-managed"

def _session() -> dict:
    """Refresh a live assignment's short-lived proxy token before kernel calls."""
    store = StateStore()
    saved = store.get(SESSION)
    client = Client(Prod(), get_credentials())
    assignments = client.list_assignments()
    if saved:
        matches = [assignment for assignment in assignments if assignment.endpoint == saved.endpoint]
    else:
        matches = [assignment for assignment in assignments if assignment.accelerator.value != "NONE"]
        if len(assignments) != 1 or len(matches) != 1:
            raise RuntimeError("Cannot adopt ambiguous or missing Colab assignment; inspect `colab sessions`")
    if len(matches) != 1:
        raise RuntimeError("Named Colab assignment is not active on provider")
    assignment = matches[0]
    if saved is None:
        saved = SessionState(name=SESSION, token=assignment.runtime_proxy_info.token,
                             url=assignment.runtime_proxy_info.url, endpoint=assignment.endpoint,
                             variant=assignment.variant.name, accelerator=assignment.accelerator.value,
                             machine_shape=assignment.machine_shape.name)
    else:
        saved.token = assignment.runtime_proxy_info.token
        saved.url = assignment.runtime_proxy_info.url
    store.add(saved)
    return {"endpoint": assignment.endpoint, "accelerator": assignment.accelerator.value,
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
    if meta.get("accelerator") not in ("CPU", "T4", "L4", "G4", "A100", "H100", "V5E1", "V6E1"):
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


def _cli(args: list[str], *, timeout: int = 120, input: str | None = None) -> str:
    if args[0] in ("exec", "upload", "download"):
        _session()
    result = subprocess.run(CLI + args, input=input, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"Colab command {args[0]} failed (exit {result.returncode}): {result.stdout[-1000:]}")
    return result.stdout


def _exec(code: str, timeout: int = 60) -> str:
    return _cli(["exec", "-s", SESSION, "--timeout", str(timeout)], input=code,
                timeout=timeout + 30)


def _remote_json(code: str) -> dict:
    output = _exec(code)
    for line in reversed(output.splitlines()):
        if line.startswith("COLAB_JSON:"):
            return json.loads(line.removeprefix("COLAB_JSON:"))
    raise RuntimeError("Remote command did not return expected JSON: " + output[-500:])


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
    existing = _remote_file(remote)
    if existing["size"] == size and existing["sha256"] == digest:
        return {"path": remote, "size": size, "sha256": digest, "resumed": True}
    _exec("import os; os.makedirs(" + repr(str(Path(remote).parent)) + ",exist_ok=True)")
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
    accelerator = meta["accelerator"]
    with (run / "worker.log").open("a", buffering=1) as log:
        try:
            if not reuse:
                args = ["new", "-s", SESSION]
                if accelerator.startswith("V"):
                    args += ["--tpu", accelerator.lower()]
                elif accelerator != "CPU":
                    args += ["--gpu", accelerator]
                log.write(_cli(args, timeout=240))
            session = _session()
            if session["accelerator"] != accelerator:
                raise RuntimeError("Live Colab accelerator does not match project metadata")
            probe = _probe(accelerator)
            log.write(probe + "\n")
            _save(run / "status.json", {"state": "staging", "accelerator": accelerator, "probe": probe, "started": time.time()})
            for item in meta.get("inputs", []):
                result = stage(project, item)
                log.write(f"Staged {item}: {result['files']} files, {result['bytes']} bytes\n")
            remote_source = f"{REMOTE_ROOT}/{project}/{source.name}"
            _transfer(source, remote_source)
            remote = f"{REMOTE_ROOT}/{project}"
            # Launch a detached subprocess; the short kernel call returns before training.
            launcher = """import os,sys,json,subprocess,time
root=ROOT; src=SOURCE
os.makedirs(root,exist_ok=True)
log=open(root+'/train.log','ab',buffering=0)
state=root+'/result.json'
script='import json,os,subprocess,sys,time\\nroot='+repr(root)+'\\np=subprocess.run([sys.executable,'+repr(src)+'],cwd="/content",stdout=open(root+"/train.log","ab",buffering=0),stderr=subprocess.STDOUT)\\njson.dump({"state":"complete" if p.returncode==0 else "error","exit_code":p.returncode,"finished":time.time()},open(root+"/result.json","w"))'
proc=subprocess.Popen([sys.executable,'-c',script],stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
if not os.path.exists(state): json.dump({'state':'running','pid':proc.pid,'started':time.time()},open(state,'w'))
print('COLAB_JSON:'+json.dumps({'pid':proc.pid,'state':'running'}),flush=True)
""".replace("ROOT", repr(remote)).replace("SOURCE", repr(remote_source))
            result = _remote_json(launcher)
            _save(run / "status.json", {"state": "running", "accelerator": accelerator,
                                         "remote": remote, "pid": result["pid"], "started": time.time()})
        except Exception as exc:
            message = f"{type(exc).__name__}: {str(exc)[-500:]}"
            log.write(message + "\n")
            _save(run / "status.json", {"state": "error", "error": message, "finished": time.time()})


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
        remote = state["remote"]
        code = "import os,json; p=" + repr(remote) + "; s=json.load(open(p+'/result.json')); pid=s.get('pid'); alive=pid and os.path.exists('/proc/'+str(pid)) and open('/proc/'+str(pid)+'/stat').read().split()[2]!='Z'; print('COLAB_JSON:'+json.dumps(s if s['state']!='running' or alive else {'state':'error','error':'Remote training process exited without result'}),flush=True)"
        try:
            current = _remote_json(code)
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
    _cli(["stop", "-s", SESSION], timeout=120)
    (STATE / "active.json").unlink()
    run = Path(active["run"])
    previous = _load(run / "status.json")
    if previous["state"] not in ("complete", "error"):
        _save(run / "status.json", {**previous, "state": "cancelled", "finished": time.time()})
    return {"released": True, "run": str(run)}




def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", default="serve", choices=("serve", "worker", "save", "stage", "push", "status", "logs", "wait", "pull", "release", "cancel"))
    parser.add_argument("values", nargs="*")
    args = parser.parse_args()
    if args.command == "serve":
        app.run()
    elif args.command == "worker":
        _worker(args.values[0], Path(args.values[1]), bool(int(args.values[2])))
    elif args.command == "push":
        output = push(args.values[0], reuse=args.values[1].lower() not in ("false", "0") if len(args.values) > 1 else True)
        print(json.dumps(output))
    elif args.command in ("logs", "wait"):
        output = globals()[args.command](args.values[0], int(args.values[1]) if len(args.values) > 1 else (40 if args.command == "logs" else 300))
        print(json.dumps(output) if not isinstance(output, str) else output)
    else:
        output = globals()[args.command](*args.values)
        print(json.dumps(output) if not isinstance(output, str) else output)


if __name__ == "__main__":
    main()
