"""renderer/scenecache.py: finished view scenes kept between commands, so looking again at an unchanged target skips the Lune build.
One entry per (project, target, scene flags): the whole world when no target is given, one model when it is. An entry is valid until
a file under the project's src/, assets/ or tests/, or under this tool's core/ and renderer/view.luau, changes. --cframe and --fov only
change the camera, so they are not part of the key (main.py applies them to the cached scene). --no-cache (or RBXMCP_NO_CACHE=1) skips it."""
import hashlib, json, os, shutil, time
from pathlib import Path

REPO = Path(os.path.abspath(__file__)).parent.parent
SOURCE_ROOTS = ("src", "assets", "tests")
TOOL_PATHS = (REPO / "renderer" / "view.luau", REPO / "renderer" / "scene.luau", REPO / "core")
CAMERA_ONLY = ("--cframe=", "--fov=")


def enabled() -> bool:
    return not os.environ.get("RBXMCP_NO_CACHE")


def home() -> Path:
    env = os.environ.get("RBXMCP_HOME")
    base = Path(env) if env else (Path(os.environ["LOCALAPPDATA"]) / "rbxmcp" if os.environ.get("LOCALAPPDATA") else Path.home() / ".cache" / "rbxmcp")
    return base / "scenes"


def _walk(path: Path, acc: list) -> None:
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    _walk(Path(entry.path), acc)
                else:
                    acc[0] += 1
                    acc[1] = max(acc[1], entry.stat().st_mtime_ns)
    except OSError:
        pass


def fingerprint(project) -> str:
    acc = [0, 0]
    for root in SOURCE_ROOTS:
        _walk(Path(project) / root, acc)
    for item in TOOL_PATHS:
        if item.is_dir():
            _walk(item, acc)
        elif item.exists():
            acc[0] += 1
            acc[1] = max(acc[1], item.stat().st_mtime_ns)
    return f"{acc[0]}:{acc[1]}"


def scene_flags(flags) -> list:
    return [f for f in flags if not f.startswith(CAMERA_ONLY)]


def _key(project, target, flags) -> str:
    return hashlib.sha1(json.dumps([str(Path(project).resolve()), target, sorted(scene_flags(flags))]).encode()).hexdigest()[:16]


def lookup(project, target, flags):
    """the cached scene folder if no source has changed since it was built, else None"""
    try:
        pointer = json.loads((home() / f"{_key(project, target, flags)}.json").read_text())
        folder = Path(pointer["dir"])
        if pointer["stamp"] == fingerprint(project) and (folder / "manifest.json").exists():
            return folder
    except (OSError, ValueError, KeyError):
        pass
    return None


def new_dir(project, target, flags) -> Path:
    folder = home() / f"{_key(project, target, flags)}-{int(time.time() * 1000)}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def commit(project, target, flags, folder: Path, stamp: str) -> None:
    """`stamp` is the fingerprint taken BEFORE the build began, so an edit during the build leaves the entry stale, not wrong"""
    pointer_file = home() / f"{_key(project, target, flags)}.json"
    old = None
    try:
        old = json.loads(pointer_file.read_text()).get("dir")
    except (OSError, ValueError):
        pass
    pointer_file.write_text(json.dumps({"dir": str(folder), "stamp": stamp, "project": str(project), "target": target}))
    if old and old != str(folder):
        shutil.rmtree(old, ignore_errors=True)
