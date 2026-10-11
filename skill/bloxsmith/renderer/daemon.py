"""renderer/daemon.py: keeps one browser open between commands, so a look costs a second or two instead of a cold start.
main.py uses RemoteRenderer, which starts the daemon the first time and talks to it after that. nothing to run by hand.
  python renderer/daemon.py serve   run the daemon (main.py does this, detached)
  python renderer/daemon.py stop    ask a running daemon to exit
Reload: it exits when any renderer source file changes, and the next command starts a fresh one (--no-reload / RBXMCP_NO_RELOAD=1 keeps the old one). --no-daemon / RBXMCP_NO_DAEMON=1 skips the daemon and uses a one-off browser.
set RBXMCP_DAEMON_WINDOW=1 to run it in a visible console window (Windows). It exits by itself after IDLE seconds without a request, or when the renderer source changes."""
import base64, http.server, json, os, subprocess, sys, time, urllib.error, urllib.request
from pathlib import Path

HERE = Path(os.path.abspath(__file__)).parent
IDLE = 15 * 60


def home() -> Path:
    env = os.environ.get("RBXMCP_HOME")
    if env:
        return Path(env)
    if os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "rbxmcp"
    return Path.home() / ".cache" / "rbxmcp"


def info_file() -> Path:
    return home() / "render.json"


def _stamp() -> float:
    """newest mtime of the renderer's own source (python and the web page it loads). when it moves, a running daemon is stale"""
    newest = 0.0
    for folder, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in ("node_modules", "__pycache__", "rbxcontent")]
        for name in files:
            if name.endswith((".py", ".html", ".js", ".css", ".mjs")):
                newest = max(newest, os.stat(os.path.join(folder, name)).st_mtime)
    return newest


def serve() -> None:
    sys.path.insert(0, str(HERE))
    from shot import Renderer
    renderer = Renderer()
    started_for = _stamp()
    last = [time.time()]
    quit_now = [False]

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, payload):
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            last[0] = time.time()
            if self.path == "/quit":
                quit_now[0] = True
            self._send({"ok": True, "stamp": started_for, "pid": os.getpid()})

        def do_POST(self):
            last[0] = time.time()
            began = time.time()
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            try:
                images = renderer.views(body["scene"], body["views"], topbar=body["topbar"], nofog=body["nofog"])
                payload = {"images": [[name, base64.b64encode(data).decode()] for name, data in images]}
            except Exception as error:
                payload = {"error": str(error)}
            last[0] = time.time()
            print(f"rendered {len(body['views'])} view(s) of {os.path.basename(body['scene'])} in {time.time() - began:.1f}s", flush=True)
            self._send(payload)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 2
    info_file().parent.mkdir(parents=True, exist_ok=True)
    info_file().write_text(json.dumps({"port": server.server_address[1], "pid": os.getpid(), "stamp": started_for}))
    try:
        while not quit_now[0] and time.time() - last[0] < IDLE and (_stamp() == started_for or os.environ.get("RBXMCP_NO_RELOAD")):
            server.handle_request()
    finally:
        try:
            info_file().unlink()
        except OSError:
            pass
        renderer.close()


def _read_info():
    try:
        return json.loads(info_file().read_text())
    except (OSError, ValueError):
        return None


def _ping(port: int):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=3) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def stop() -> bool:
    info = _read_info()
    if not info:
        return False
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{info['port']}/quit", timeout=3).read()
    except (urllib.error.URLError, OSError):
        pass
    return True


def _start() -> int:
    home().mkdir(parents=True, exist_ok=True)
    if os.environ.get("RBXMCP_DAEMON_WINDOW") and os.name == "nt":
        subprocess.Popen([sys.executable, str(HERE / "daemon.py"), "serve"], cwd=str(home()), creationflags=0x00000010, close_fds=True)
    else:
        log = open(home() / "render.log", "ab") if home().exists() else subprocess.DEVNULL
        flags = 0x00000008 | 0x00000200 | 0x08000000 if os.name == "nt" else 0
        subprocess.Popen([sys.executable, str(HERE / "daemon.py"), "serve"], cwd=str(home()), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         creationflags=flags, close_fds=True)
    deadline = time.time() + 40
    while time.time() < deadline:
        info = _read_info()
        if info and _ping(info["port"]):
            return info["port"]
        time.sleep(0.2)
    raise RuntimeError("the render daemon did not start (see render.log in the rbxmcp folder)")


def _port() -> int:
    info = _read_info()
    if info and _ping(info["port"]) and (info.get("stamp") == _stamp() or os.environ.get("RBXMCP_NO_RELOAD")):
        return info["port"]
    if info:
        stop()
        time.sleep(0.5)
    home().mkdir(parents=True, exist_ok=True)
    return _start()


class RemoteRenderer:
    """same views() as shot.Renderer, drawn by the daemon's already-open browser. falls back to a browser of its own if the daemon will not run."""

    def __init__(self):
        self.local = None

    def views(self, scene_path, views, topbar=False, nofog=True):
        if self.local is None and not os.environ.get("RBXMCP_NO_DAEMON"):
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{_port()}/render",
                    data=json.dumps({"scene": os.path.abspath(scene_path), "views": views, "topbar": topbar, "nofog": nofog}).encode(),
                    method="POST")
                with urllib.request.urlopen(request, timeout=300) as response:
                    payload = json.loads(response.read())
                if "error" in payload:
                    raise RuntimeError(payload["error"])
                return [(name, base64.b64decode(data)) for name, data in payload["images"]]
            except (urllib.error.URLError, OSError, RuntimeError) as error:
                if isinstance(error, RuntimeError) and "daemon" not in str(error):
                    raise
                print(f"note: render daemon unavailable ({error}); using a one-off browser (slower)")
        from shot import Renderer
        self.local = self.local or Renderer()
        return self.local.views(scene_path, views, topbar=topbar, nofog=nofog)

    def close(self):
        if self.local is not None:
            self.local.close()
            self.local = None


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "serve":
        serve()
    elif command == "stop":
        print("stopped" if stop() else "no daemon running")
    else:
        print(__doc__)
