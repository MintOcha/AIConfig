"""Verified version-scoped Kaggle SSE logs for the MCP process."""
import asyncio
from collections import deque
import json
import sys

import time
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
                    if time.monotonic() - self.accessed >= 90:
                        break
                    continue
                except ValueError as error:
                    raise RuntimeError('Kaggle log worker produced an oversized line; inspect the session log directly') from error
                if not raw:
                    break
                try:
                    self.lines.append(json.loads(raw))
                except json.JSONDecodeError as error:
                    raise RuntimeError(f'Kaggle log worker returned non-JSON stdout: {raw[:200].decode(errors="replace").strip()!r}') from error
                self.last_line_at = time.monotonic()
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


async def logs(ref, version, env, wait=8):
    key = ref, version
    stream = streams.get(key)
    if stream is None or stream.task.done():
        stream = streams[key] = LogStream(ref, version, env)
    stream.accessed = time.monotonic()
    deadline = stream.accessed + wait
    if not stream.ready.is_set():
        try:
            await asyncio.wait_for(stream.ready.wait(), wait)
        except TimeoutError:
            pass
    # The stream replays historical events in a burst. Returning on its first
    # event discards the rest when a one-shot CLI invocation exits.
    while stream.last_line_at is not None and not stream.task.done():
        remaining = deadline - time.monotonic()
        idle = 0.5 - (time.monotonic() - stream.last_line_at)
        if remaining <= 0 or idle <= 0:
            break
        await asyncio.sleep(min(remaining, idle))
    return list(stream.lines), stream.error


async def close(ref, version):
    stream = streams.pop((ref, version), None)
    if stream:
        await stream.stop()
