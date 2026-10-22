"""Verified version-scoped Kaggle SSE logs for the MCP process."""
import asyncio
from collections import deque
import json
from pathlib import Path
import sys
import time

CACHE_DIR = Path('/tmp/kaggle_logs_cache')
CACHE_DIR.mkdir(parents=True, exist_ok=True)
STREAM = r'''
import json, re, sys
from contextlib import redirect_stdout
from urllib.parse import urlparse
with redirect_stdout(sys.stderr):
    from kaggle.api.kaggle_api_extended import KaggleApi
api = KaggleApi()
with redirect_stdout(sys.stderr):
    api.authenticate()
owner, slug = sys.argv[1].split('/')
version = int(sys.argv[2])
with api.build_kaggle_client() as client:
    http = client._http_client
    http._init_session()
    with http._session.get(f'https://api.kaggle.com/v1/kernels/output/download/{owner}/{slug}',
                           params={'versionNumber': version}, stream=True,
                           allow_redirects=False, timeout=20) as response:
        if response.status_code != 302:
            raise RuntimeError(f'Kaggle output redirect for {owner}/{slug} v{version} returned HTTP {response.status_code}: {response.raw.read(200, decode_content=True).decode(errors="replace")!r}')
        location = urlparse(response.headers.get('Location', ''))
        match = re.fullmatch(r'/v1/kernels/output/download_zip/([1-9][0-9]*)', location.path)
        if location.scheme != 'https' or location.hostname != 'api.kaggle.com' or not match:
            raise RuntimeError(f'Kaggle output redirect for {owner}/{slug} v{version} did not identify a verified session')
        session_id = int(match[1])
    with http._session.get(f'https://api.kaggle.com/v1/kernels/sessions/{session_id}/logs/stream',
                           stream=True, timeout=(10, 90)) as response:
        if not response.ok:
            raise RuntimeError(f'Kaggle log stream for {owner}/{slug} v{version} returned HTTP {response.status_code}: {response.raw.read(200, decode_content=True).decode(errors="replace")!r}')
        content_type = response.headers.get('Content-Type', '').lower()
        if not content_type.startswith(('text/event-stream', 'application/json', 'text/plain')):
            raise RuntimeError(f'Kaggle log stream for {owner}/{slug} v{version} returned unexpected Content-Type {content_type!r}: {response.raw.read(200, decode_content=True).decode(errors="replace")!r}')
        events = (api._iter_sse_events(response) if content_type.startswith('text/event-stream')
                  else api._iter_blob_lines(response))
        for event in events:
            for line in str(event.get('data', '')).splitlines():
                print(json.dumps(line), flush=True)
'''


class LogStream:
    def __init__(self, ref, version, env):
        self.ref = ref
        self.version = version
        self.env = env
        self.accessed = time.monotonic()
        self.lines = deque(maxlen=200)
        self.cache_file = CACHE_DIR / f"{ref.replace('/', '_')}_v{version}.log"
        if self.cache_file.exists():
            try:
                cached = [line.strip() for line in self.cache_file.read_text(errors='replace').splitlines() if line.strip()]
                self.lines.extend(cached[-200:])
            except Exception:
                pass
        self.last_line_at = None
        self.error = None
        self.ready = asyncio.Event()
        self.process = None
        self.task = asyncio.create_task(self.collect())

    async def collect(self):
        process = None
        try:
            process = await asyncio.create_subprocess_exec(sys.executable, '-u', '-c', STREAM,
                self.ref, str(self.version), env=self.env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=1024 * 1024)
            self.process = process
            while True:
                try:
                    async with asyncio.timeout(90):
                        raw = await process.stdout.readline()
                except TimeoutError:
                    # Keep collecting continuously for active runs without disconnecting
                    continue
                except ValueError as error:
                    raise RuntimeError('Kaggle log worker produced an oversized line; inspect the session log directly') from error
                if not raw:
                    break
                try:
                    parsed_line = json.loads(raw)
                    self.lines.append(parsed_line)
                    with self.cache_file.open('a', encoding='utf-8') as f:
                        f.write(parsed_line + '\n')
                except json.JSONDecodeError as error:
                    raise RuntimeError(f'Kaggle log worker returned non-JSON stdout: {raw[:200].decode(errors="replace").strip()!r}') from error
                self.ready.set()
            await process.wait()
            if process.returncode:
                self.error = (await process.stderr.read()).decode(errors='replace')[-1000:].strip() or f'Kaggle log worker exited {process.returncode}'
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.error = str(error)
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            self.process = None
            self.ready.set()

    async def stop(self):
        if self.process is not None and self.process.returncode is None:
            self.process.kill()
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


streams = {}


async def logs(ref, version, env, wait=0, background=True):
    key = ref, version
    cache_file = CACHE_DIR / f"{ref.replace('/', '_')}_v{version}.log"
    if not background:
        if cache_file.exists():
            try:
                lines = [line.strip() for line in cache_file.read_text(errors='replace').splitlines() if line.strip()]
                return lines[-200:], None
            except Exception:
                pass
        return [], None
    stream = streams.get(key)
    if stream is None or stream.task.done():
        stream = streams[key] = LogStream(ref, version, env)
    stream.accessed = time.monotonic()
    if not stream.lines and wait > 0 and not stream.ready.is_set():
        try:
            await asyncio.wait_for(stream.ready.wait(), wait)
        except TimeoutError:
            pass
    if not stream.lines and cache_file.exists():
        try:
            lines = [line.strip() for line in cache_file.read_text(errors='replace').splitlines() if line.strip()]
            return lines[-200:], stream.error
        except Exception:
            pass
    return list(stream.lines), stream.error


async def close(ref, version):
    stream = streams.pop((ref, version), None)
    if stream:
        await stream.stop()
