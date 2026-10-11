#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pillow>=11.0.0",
#   "playwright>=1.40.0",
#   "mcp>=1.0.0",
# ]
# ///
"""rbxmcp: build, test and look at a Roblox project without Studio.

    rbxmcp set-project <dir>        pick the default project (once)
    rbxmcp status
    rbxmcp build [path]
    rbxmcp test [name-filter]
    rbxmcp view [target]
    rbxmcp toolbox <query> [--category=Model|Decal|Audio|Plugin] [--limit=10]
    rbxmcp catalog <query> [--category=All|Clothing|Collectibles] [--limit=10]
    rbxmcp avatar [userId]
    rbxmcp pull <assetId> [--output=assets/Name.rbxm]
    rbxmcp upload <file> [--name=Name] [--creatorId=123] [--assetType=Model]
    rbxmcp publish <assetId>
    add  --no-daemon  to any command to skip the render daemon (a warm browser kept between commands; it reloads itself when the rbxmcp renderer source changes)
    add  --no-cache  to view: always rebuild the scene (finished scenes are otherwise kept until a source file changes)
    add  --no-reload  to keep a running daemon even if the rbxmcp renderer source has changed since it started
    add  --project <dir>  (or set RBXMCP_PROJECT) to run ONE command against another project without changing the default.

One project is selected at a time (set-project), or inferred from the current directory if it has a src/ folder.
The selection lives in the cache folder (RBXMCP_HOME, default %LOCALAPPDATA%/rbxmcp or ~/.cache/rbxmcp),
together with auto-downloaded Lune and renderer content."""
import io
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import urllib.request
import zipfile

REPO = Path(os.path.abspath(__file__)).parent
LUNE_VERSION = "0.10.5"


def home() -> Path:
    env = os.environ.get("RBXMCP_HOME")
    if env:
        base = Path(env)
    elif os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"]) / "rbxmcp"
    else:
        base = Path.home() / ".cache" / "rbxmcp"
    base.mkdir(parents=True, exist_ok=True)
    return base


def runtime_repo() -> Path:
    """If REPO is on a Windows UNC share (\\\\...), mirror runtime scripts to local cache so Lune/browser never run on UNC."""
    if os.name == "nt" and str(REPO).startswith("\\\\"):
        dst = home() / "runtime"
        for sub in ("core", "tester", "renderer", "tools"):
            src_dir = REPO / sub
            dst_dir = dst / sub
            if src_dir.is_dir():
                shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        return dst
    return REPO


def ensure_lune() -> Path:
    exe = "lune.exe" if os.name == "nt" else "lune"
    env_lune = os.environ.get("RBXMCP_LUNE")
    if env_lune and Path(env_lune).is_file():
        return Path(env_lune)
    candidates = [
        REPO / "tools" / "bin" / exe,
        home() / "bin" / exe,
        Path.home() / ".lune" / "bin" / exe,
        Path.home() / ".aftman" / "bin" / exe,
        Path.home() / ".rokit" / "bin" / exe,
        Path.home() / ".cargo" / "bin" / exe,
    ]
    which_lune = shutil.which("lune")
    if which_lune:
        candidates.insert(2, Path(which_lune))
    for cand in candidates:
        if cand.is_file():
            return cand

    sys_name = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}.get(platform.system(), "linux")
    machine = platform.machine().lower()
    arch = "aarch64" if machine in ("arm64", "aarch64") else "x86_64"
    url = f"https://github.com/lune-org/lune/releases/download/v{LUNE_VERSION}/lune-{LUNE_VERSION}-{sys_name}-{arch}.zip"
    target = home() / "bin" / exe
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading lune {LUNE_VERSION} ({sys_name}-{arch})...")
    data = urllib.request.urlopen(url, timeout=60).read()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for member in zf.namelist():
            if Path(member).name == exe:
                target.write_bytes(zf.read(member))
                break
    if os.name != "nt" and target.is_file():
        target.chmod(0o755)
    return target


def load_config() -> dict:
    path = home() / "config.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_config(config: dict) -> None:
    (home() / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")


def fail(message: str) -> None:
    print(message)
    sys.exit(1)


PROJECT_OVERRIDE = None


def current_project(announce: bool = True) -> Path:
    override = PROJECT_OVERRIDE or os.environ.get("RBXMCP_PROJECT")
    if override:
        path = Path(override).resolve()
        if not (path / "src").is_dir():
            fail(f"--project {path} has no src/ folder")
        print(f"project: {path.name} ({path}) [--project, this call only]")
        return path
    cwd = Path.cwd().resolve()
    if (cwd / "src").is_dir() and cwd != REPO.resolve():
        return cwd
    config = load_config()
    project = config.get("project")
    if not project:
        fail("no project selected. run: set-project <dir>   (or add --project <dir> to this command)")
    path = Path(project)
    if not (path / "src").is_dir():
        fail(f"project {path} has no src/ folder (moved or deleted?). run: set-project <dir>   (or add --project <dir>)")
    if announce and config.get("announced") != project:
        print(f"project: {path.name} ({path})")
        config["announced"] = project
        save_config(config)
    return path


def lune(script: Path, *args: str) -> int:
    lune_bin = ensure_lune()
    if not lune_bin.exists():
        fail(f"lune not found at {lune_bin}")
    process = subprocess.Popen([str(lune_bin), "run", str(script), *args], cwd=str(home()), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace", bufsize=1)
    missing = False
    for line in process.stdout:
        print(line, end="", flush=True)
        if "no such file or folder" in line or "no such path" in line:
            missing = True
    code = process.wait()
    if code != 0 and missing:
        try:
            used = current_project(announce=False)
        except SystemExit:
            used = "?"
        print(f"hint: this ran against {used}. if that is the wrong project, add: --project <dir>  (applies to that one command)")
    return code


def cmd_set_project(args: list[str]) -> int:
    if not args:
        fail("usage: set-project <dir>")
    path = Path(args[0]).resolve()
    if not (path / "src").is_dir():
        fail(f"{path} has no src/ folder")
    config = load_config()
    config["project"] = str(path)
    config["announced"] = str(path)
    save_config(config)
    print(f"project set: {path.name} ({path})")
    return 0


def cmd_status(_: list[str]) -> int:
    config = load_config()
    project = config.get("project")
    lune_bin = ensure_lune()
    print(f"project: {project or 'none'}")
    print(f"cache: {home()}")
    print(f"lune: {'ok' if lune_bin.exists() else 'missing'} ({lune_bin})")
    return 0


def cmd_build(args: list[str]) -> int:
    """build            whole game -> dist/game.rbxl
    build <path>     one file or folder under world/ or src/ -> dist/<path>.rbxm"""
    project = current_project()
    rrepo = runtime_repo()
    rc = lune(rrepo / "core" / "build_main.luau", str(project).replace("\\", "/"), *args[:1])
    return rc


def cmd_test(args: list[str]) -> int:
    import json as _json
    project = current_project()
    rrepo = runtime_repo()
    snaps = project / "dist" / "snapshots"
    shutil.rmtree(snaps, ignore_errors=True)
    rc = lune(rrepo / "tester" / "run.luau", str(project).replace("\\", "/"), *args[:1])
    requests = snaps / "requests.json"
    if requests.exists():
        by_test = {}
        for r in _json.loads(requests.read_text(encoding="utf-8")):
            by_test.setdefault(r.get("test") or "test", []).append(r)
        sys.path.insert(0, str(rrepo / "renderer"))
        from daemon import RemoteRenderer
        renderer = RemoteRenderer()
        for test, items in by_test.items():
            cells = []
            for r in items:
                snap_file = project / r["file"]
                if not snap_file.exists():
                    continue
                images = renderer.views(snap_file, r["views"], topbar=r["topbar"], nofog=True)
                for sub, data in images:
                    cells.append((f"t={r.get('t', 0):.1f}s {r['label']}" + (f" {sub}" if sub else ""), data))
            if cells:
                cols = min(len(cells), 3 if len(cells) <= 12 else 4)
                size = (640, 360) if cols <= 3 else (480, 270)
                path = project / "dist" / "view" / f"{_safe(test)}_filmstrip.png"
                path.parent.mkdir(parents=True, exist_ok=True)
                _sheet(cells, cols, *size).save(path)
                print(f"filmstrip {test}: {len(cells)} frames, {cols} per row")
                print(f"  {path}")
                print("  " + " | ".join(label for label, _ in cells))
        renderer.close()
        shutil.rmtree(snaps, ignore_errors=True)
    return rc


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in name) or "world"


def _sheet(images, cols, cell_w, cell_h):
    """contact sheet of (label, png bytes) with the label drawn in each corner"""
    from PIL import Image, ImageDraw
    rows = (len(images) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell_w, rows * cell_h), (24, 24, 24))
    for i, (label, data) in enumerate(images):
        im = Image.open(io.BytesIO(data)).convert("RGB").resize((cell_w, cell_h))
        d = ImageDraw.Draw(im)
        d.rectangle([0, 0, 8 * len(label) + 10, 16], fill=(0, 0, 0))
        d.text((5, 2), label, fill=(255, 255, 255))
        sheet.paste(im, ((i % cols) * cell_w, (i // cols) * cell_h))
    return sheet


def _look_cframe(text: str) -> str:
    """'x,y,z>tx,ty,tz' (eye > target) -> the 12 numbers of a --cframe looking from the eye at the target, +Y up"""
    import math
    try:
        eye, target = ([float(v) for v in part.split(",")] for part in text.split(">"))
        assert len(eye) == 3 and len(target) == 3
    except Exception:
        fail("--look needs eye>target: --look=x,y,z>tx,ty,tz")
    f = [t - e for e, t in zip(eye, target)]
    n = math.sqrt(sum(c * c for c in f)) or 1.0
    f = [c / n for c in f]
    r = [-f[2], 0.0, f[0]]  # forward x up(0,1,0)
    n = math.sqrt(sum(c * c for c in r))
    if n < 1e-6:  # looking straight up or down: pick any right vector
        r, n = [1.0, 0.0, 0.0], 1.0
    r = [c / n for c in r]
    u = [r[1] * f[2] - r[2] * f[1], r[2] * f[0] - r[0] * f[2], r[0] * f[1] - r[1] * f[0]]
    b = [-c for c in f]
    return ",".join(f"{v:.4f}" for v in (*eye, r[0], u[0], b[0], r[1], u[1], b[1], r[2], u[2], b[2]))


def cmd_view(args: list[str]) -> int:
    """view [target] [--cframe=x,y,z,r00..r22] [--look=x,y,z>tx,ty,tz] [--angles=top,+x] [--fov=55] [--out=name]
    target: dotted path in the built project (Workspace.Coins, StarterGui.HUD). none = the whole world.
    world targets get 9 angles as one contact sheet; gui targets get desktop and phone.
    --look is a camera at an eye point looking at a target (repeat it for several). --out names the picture."""
    import json as _json
    import tempfile
    import time
    project = current_project()
    rrepo = runtime_repo()
    flags = [a for a in args if a.startswith("--")]
    flags = [("--cframe=" + _look_cframe(f.split("=", 1)[1])) if f.startswith("--look=") else f for f in flags]
    out_name = next((f.split("=", 1)[1] for f in flags if f.startswith("--out=")), None)
    flags = [f for f in flags if not f.startswith("--out=")]
    positional = [a for a in args if not a.startswith("--")]
    target = positional[0] if positional else ""
    out = project / "dist" / "view"
    out.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(rrepo / "renderer"))
    import scenecache
    use_cache = scenecache.enabled()
    t0 = time.time()
    work = scenecache.lookup(project, target, flags) if use_cache else None
    if work is not None:
        print("scene: cached (no source changed since it was built)")
    else:
        stamp = scenecache.fingerprint(project) if use_cache else None
        work = scenecache.new_dir(project, target, flags) if use_cache else Path(tempfile.mkdtemp(prefix="work-", dir=out))
        rc = lune(rrepo / "renderer" / "view.luau", str(project).replace("\\", "/"), str(work).replace("\\", "/"), target, *scenecache.scene_flags(flags))
        if rc != 0:
            shutil.rmtree(work, ignore_errors=True)
            return rc
        if use_cache:
            scenecache.commit(project, target, flags, work, stamp)
    manifest = _json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    fov = next((float(f.split("=", 1)[1]) for f in flags if f.startswith("--fov=")), None)
    cframes = [f.split("=", 1)[1] for f in flags if f.startswith("--cframe=")]
    for scene in manifest["scenes"]:
        if scene["kind"] != "world":
            continue
        if cframes:
            scene["views"] = []
            for index, text in enumerate(cframes, 1):
                numbers = [float(x) for x in text.split(",")]
                if len(numbers) != 12:
                    fail("--cframe needs 12 numbers: x,y,z,r00,r01,r02,r10,r11,r12,r20,r21,r22")
                scene["views"].append({"name": f"custom{index}" if len(cframes) > 1 else "custom", "cframe": numbers, "fov": fov or 55})
        elif fov:
            for v in scene["views"]:
                v["fov"] = fov
    built_s = time.time() - t0
    parts = manifest.get("parts", 0)
    from daemon import RemoteRenderer
    renderer = RemoteRenderer()
    name = _safe(out_name) if out_name else _safe(target)
    written = []
    try:
        for scene in manifest["scenes"]:
            if scene["kind"] == "world":
                images = renderer.views(scene["file"], scene["views"], topbar=False, nofog=True)
                if len(images) == 1:
                    path = out / f"{name}_{_safe(images[0][0])}.png"
                    path.write_bytes(images[0][1])
                    written.append(str(path))
                else:
                    sheet = _sheet(images, 3, 640, 360)
                    sheet.save(out / f"{name}_sheet.png")
                    order = " ".join(label for label, _ in images)
                    written.append(f"{out / (name + '_sheet.png')} (3 per row: {order})")
            else:
                images = renderer.views(scene["file"], scene["views"], topbar=True, nofog=True)
                written.append((scene["name"], images[0][1]))
        guis = [w for w in written if isinstance(w, tuple)]
        written = [w for w in written if isinstance(w, str)]
        if guis:
            from PIL import Image
            ims = [Image.open(io.BytesIO(d)).convert("RGB") for _, d in guis]
            canvas = Image.new("RGB", (max(i.width for i in ims), sum(i.height for i in ims)), (24, 24, 24))
            y = 0
            for i in ims:
                canvas.paste(i, (0, y))
                y += i.height
            canvas.save(out / f"{name}_gui.png")
            written.append(f"{out / (name + '_gui.png')} (desktop 1280x720 above phone 844x390)")
    finally:
        renderer.close()
        if not use_cache:
            shutil.rmtree(work, ignore_errors=True)
    for note in manifest.get("notes", []):
        print(note)
    drawn = f", {parts} parts" if parts else ""
    print(f"view {target or 'world'}: {round(time.time() - t0, 1)}s (scene {round(built_s, 1)}s, draw {round(time.time() - t0 - built_s, 1)}s){drawn}")
    if manifest["scenes"] and any(sc["kind"] == "world" for sc in manifest["scenes"]) and parts == 0:
        print("warning: nothing physical was drawn (0 parts). the target may be empty, or only GUI or scripts.")
    for w in written:
        print("  " + w)
    return 0


def cmd_toolbox(args: list[str]) -> int:
    """toolbox <query> [--category=Model|Decal|Audio|Plugin] [--limit=10]"""
    sys.path.insert(0, str(REPO))
    from tools.cloud import toolbox_search
    flags = dict(a.split("=", 1) for a in args if a.startswith("--") and "=" in a)
    query = " ".join(a for a in args if not a.startswith("--"))
    category = flags.get("--category", "Model")
    limit = int(flags.get("--limit", 10))
    results = toolbox_search(query=query, category=category, limit=limit)
    if not results:
        print(f"no toolbox results found for '{query}' ({category})")
        return 0
    print(f"toolbox ({category}, {len(results)} results):")
    for r in results:
        print(f"  [{r['id']}] {r['name']} by {r['creator']}")
    return 0


def cmd_catalog(args: list[str]) -> int:
    """catalog <query> [--category=All|Clothing|Collectibles] [--limit=10]"""
    sys.path.insert(0, str(REPO))
    from tools.cloud import catalog_search
    flags = dict(a.split("=", 1) for a in args if a.startswith("--") and "=" in a)
    query = " ".join(a for a in args if not a.startswith("--"))
    category = flags.get("--category", "All")
    limit = int(flags.get("--limit", 10))
    results = catalog_search(query=query, category=category, limit=limit)
    if not results:
        print(f"no catalog results found for '{query}'")
        return 0
    print(f"catalog ({category}, {len(results)} results):")
    for r in results:
        price = f"{r['price']} R$" if r.get("price") is not None else "Free/Offsale"
        print(f"  [{r['id']}] {r['name']} - {price} (by {r['creator']})")
    return 0


def cmd_pull(args: list[str]) -> int:
    """pull <assetId> [--output=assets/Name.rbxm]"""
    sys.path.insert(0, str(REPO))
    from tools.cloud import pull_asset
    flags = dict(a.split("=", 1) for a in args if a.startswith("--") and "=" in a)
    positional = [a for a in args if not a.startswith("--")]
    if not positional:
        fail("usage: pull <assetId> [--output=assets/Name.rbxm]")
    asset_id = positional[0]
    project = current_project()
    out = flags.get("--output")
    if not out:
        out_path = project / "assets" / f"asset_{asset_id}.rbxm"
    else:
        out_path = (project / out) if not Path(out).is_absolute() else Path(out)
    try:
        saved = pull_asset(asset_id, out_path)
        rel = saved.relative_to(project) if project in saved.parents else saved
        print(f"pulled asset {asset_id} -> {rel} ({round(saved.stat().st_size / 1024, 1)} KB)")
        print(f"hint: look at it with: rbxmcp view {rel}")
    except Exception as e:
        fail(f"pull failed: {e}")
    return 0


def cmd_upload(args: list[str]) -> int:
    """upload <file> [--name=Name] [--creatorId=123] [--creatorType=user|group] [--assetType=Model]"""
    sys.path.insert(0, str(REPO))
    from tools.cloud import upload_asset
    flags = dict(a.split("=", 1) for a in args if a.startswith("--") and "=" in a)
    positional = [a for a in args if not a.startswith("--")]
    if not positional:
        fail("usage: upload <path> [--name=Name] [--creatorId=id] [--assetType=Model]")
    target_str = positional[0]
    project = current_project()
    rrepo = runtime_repo()
    target_path = (project / target_str) if not Path(target_str).is_absolute() else Path(target_str)

    if str(target_path).endswith(".build.luau"):
        rel_path = str(target_path.relative_to(project)).replace("\\", "/")
        print(f"compiling {rel_path} to .rbxm before upload...")
        rc = lune(rrepo / "core" / "build_main.luau", str(project).replace("\\", "/"), rel_path)
        if rc != 0:
            fail("build step failed before upload")
        clean_name = rel_path.rstrip("/").removesuffix(".build.luau").replace("/", "_")
        target_path = project / "dist" / f"{clean_name}.rbxm"
        if not target_path.exists():
            fail(f"compiled artifact {target_path} not found")

    name = flags.get("--name")
    creator_id = flags.get("--creatorId")
    creator_type = flags.get("--creatorType", "user")
    asset_type = flags.get("--assetType")
    try:
        res = upload_asset(target_path, creator_id=creator_id, creator_type=creator_type, asset_type=asset_type, name=name)
        asset_id = res.get("response", {}).get("assetId") or res.get("assetId")
        print(f"uploaded {target_path.name} successfully! Asset ID: {asset_id or res.get('path')}")
    except Exception as e:
        fail(f"upload failed: {e}")
    return 0


def cmd_avatar(args: list[str]) -> int:
    """avatar [userId]"""
    sys.path.insert(0, str(REPO))
    from tools.cloud import avatar_details, get_default_user_id
    user_id = args[0] if args else get_default_user_id()
    if not user_id:
        fail("usage: avatar <userId> (or be logged into Studio)")
    try:
        info = avatar_details(user_id)
    except Exception as e:
        fail(f"avatar query failed: {e}")
    print(f"avatar for user {user_id} ({info.get('playerAvatarType', 'Unknown')}):")
    scales = info.get("scales", {})
    if scales:
        print(f"  scales: height={scales.get('height')}, width={scales.get('width')}, head={scales.get('head')}")
    assets = info.get("assets", [])
    print(f"  equipped assets ({len(assets)}):")
    for a in assets:
        atype = a.get("assetType", {}).get("name", "Asset")
        print(f"    [{a.get('id')}] {a.get('name')} ({atype})")
    return 0


def cmd_publish(args: list[str]) -> int:
    """publish <assetId>"""
    sys.path.insert(0, str(REPO))
    from tools.cloud import publish_model
    if not args:
        fail("usage: publish <assetId>")
    asset_id = args[0]
    try:
        res = publish_model(asset_id)
        print(f"published model {asset_id} to Creator Store successfully: {res}")
    except Exception as e:
        fail(f"publish failed: {e}")
    return 0


def cmd_mcp(args: list[str]) -> int:
    """mcp [--transport=stdio|sse]"""
    sys.path.insert(0, str(REPO))
    from mcp_server import create_server
    transport = "stdio"
    for a in args:
        if a.startswith("--transport="):
            transport = a.split("=", 1)[1]
    server = create_server()
    server.run(transport=transport)
    return 0


COMMANDS = {
    "set-project": cmd_set_project,
    "status": cmd_status,
    "build": cmd_build,
    "test": cmd_test,
    "view": cmd_view,
    "toolbox": cmd_toolbox,
    "catalog": cmd_catalog,
    "avatar": cmd_avatar,
    "pull": cmd_pull,
    "upload": cmd_upload,
    "publish": cmd_publish,
    "mcp": cmd_mcp,
}


def main() -> int:
    global PROJECT_OVERRIDE
    argv = sys.argv[1:]
    if "--project" in argv:
        i = argv.index("--project")
        if i + 1 >= len(argv):
            fail("--project needs a folder")
        PROJECT_OVERRIDE = argv[i + 1]
        del argv[i:i + 2]
    if "--no-daemon" in argv:
        argv.remove("--no-daemon")
        os.environ["RBXMCP_NO_DAEMON"] = "1"
    if "--no-cache" in argv:
        argv.remove("--no-cache")
        os.environ["RBXMCP_NO_CACHE"] = "1"
    if "--no-reload" in argv:
        argv.remove("--no-reload")
        os.environ["RBXMCP_NO_RELOAD"] = "1"
    if not argv or argv[0] not in COMMANDS:
        print(__doc__)
        return 2
    return COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main())
