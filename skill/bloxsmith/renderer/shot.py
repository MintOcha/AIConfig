"""renderer/shot.py <scene.json> <out.png> [--no-topbar] [--nofog] [--repeat N]
Headless screenshots of an exported scene with the three.js renderer in renderer/web.
GPU is used when available (RBXMCP_SOFTWARE_GL=1 forces software WebGL, ~4s slower per cold render).
Renderer keeps one browser alive: shot() renders the scene's own camera, views() renders several cameras from one page load."""
import base64
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from playwright.sync_api import sync_playwright

HERE = os.path.abspath(os.path.join(os.path.dirname(__file__), "web"))
sys.path.insert(0, HERE)
from fetch_content import ensure_content

TYPES = {".html": "text/html", ".js": "text/javascript", ".mjs": "text/javascript", ".json": "application/json",
         ".png": "image/png", ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf", ".otf": "font/otf", ".css": "text/css"}


class Renderer:
    def __init__(self):
        outer = self
        self.scene_path = None
        self.missing = []
        content_dir = os.path.abspath(str(ensure_content()))

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?")[0]
                if path == "/scene.json":
                    f = outer.scene_path
                elif path.startswith("/rbxcontent/"):
                    rel = path[len("/rbxcontent/"):].lstrip("/").replace("/", os.sep)
                    f = os.path.abspath(os.path.join(content_dir, rel))
                    if not f.startswith(content_dir):
                        self.send_response(403); self.end_headers(); return
                else:
                    f = os.path.abspath(os.path.join(HERE, path.lstrip("/").replace("/", os.sep)))
                    if not f.startswith(HERE):
                        self.send_response(403); self.end_headers(); return
                try:
                    with open(f, "rb") as fp:
                        body = fp.read()
                except OSError:
                    self.send_response(404); self.end_headers(); return
                self.send_response(200)
                self.send_header("content-type", TYPES.get(os.path.splitext(f)[1], "application/octet-stream"))
                self.end_headers()
                self.wfile.write(body)

            do_HEAD = do_GET

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]
        t = time.time()
        self.pw = sync_playwright().start()
        args = ["--ignore-gpu-blocklist"]
        if os.environ.get("RBXMCP_SOFTWARE_GL"):
            args += ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
        self.browser = self._launch_browser(args)
        self.launch_s = time.time() - t

    def _launch_browser(self, args):
        for channel in ("msedge", "chrome", None):
            try:
                if channel:
                    return self.pw.chromium.launch(channel=channel, args=args)
                return self.pw.chromium.launch(args=args)
            except Exception:
                continue
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
        return self.pw.chromium.launch(args=args)

    def _open(self, scene_path, topbar, nofog):
        self.scene_path = os.path.abspath(scene_path)
        with open(self.scene_path, encoding="utf-8") as fp:
            w, h = json.load(fp)["screen"]
        page = self.browser.new_page(viewport={"width": int(w), "height": int(h)})
        page.on("pageerror", lambda e: print("page error:", e))
        page.on("response", lambda r: self.missing.append(r.url.split("/", 3)[-1]) if r.status == 404 and "127.0.0.1" in r.url else None)
        page.goto(f"http://127.0.0.1:{self.port}/index.html?scene=/scene.json&topbar={1 if topbar else 0}" + ("&nofog" if nofog else ""))
        page.wait_for_function("window.__previewDone || window.__previewError", timeout=180000)
        err = page.evaluate("window.__previewError")
        if err:
            page.close()
            raise RuntimeError(err)
        return page

    def shot(self, scene_path, out, topbar=True, nofog=False):
        t0 = time.time()
        page = self._open(scene_path, topbar, nofog)
        t1 = time.time()
        page.screenshot(path=out)
        page.close()
        return {"scene_ready": t1 - t0, "screenshot": time.time() - t1}

    def views(self, scene_path, views, topbar=False, nofog=True):
        """render several cameras from one page load; returns [(name, png_bytes)]"""
        key = (os.path.abspath(scene_path), os.stat(scene_path).st_mtime_ns, topbar, nofog)
        if getattr(self, "kept_key", None) != key:
            self._drop_kept()
            self.kept = self._open(scene_path, topbar, nofog)
            self.kept_key = key
        out = []
        try:
            for v in views:
                url = self.kept.evaluate("v => window.__renderView(v)", v)
                out.append((v["name"], base64.b64decode(url.split(",", 1)[1])))
        except Exception:
            self._drop_kept()
            raise
        return out

    def _drop_kept(self):
        page, self.kept, self.kept_key = getattr(self, "kept", None), None, None
        if page is not None:
            try:
                page.close()
            except Exception:
                pass

    def close(self):
        self._drop_kept()
        self.browser.close()
        self.pw.stop()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--") and not a.isdigit()]
    repeat = int(sys.argv[sys.argv.index("--repeat") + 1]) if "--repeat" in sys.argv else 1
    r = Renderer()
    print(f"browser launch: {r.launch_s:.2f}s")
    for i in range(repeat):
        t = time.time()
        p = r.shot(args[0], args[1], topbar="--no-topbar" not in sys.argv, nofog="--nofog" in sys.argv)
        print(f"shot {i + 1}: total {time.time() - t:.2f}s  " + "  ".join(f"{k} {v:.2f}s" for k, v in p.items()))
    r.close()
