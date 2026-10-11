"""Bounded bridge to Roblox's native Studio mode proxy.

Screenshots use the isolated Windows visible-viewport worker, never this transport.
"""
import glob
import json
import os
import queue
import re
import subprocess
import threading

_CALL_TIMEOUT = 5



class StudioMCP:
    def __init__(self):
        self._lock = threading.Lock()
        self._process = None
        self._lines = None
        self._next_id = 0

    @staticmethod
    def _executable():
        found = glob.glob(os.path.expandvars(r'%LOCALAPPDATA%\Roblox\Versions\version-*\StudioMCP.exe'))
        if not found:
            raise RuntimeError('StudioMCP.exe not found; install Roblox Studio with its MCP server')
        return max(found, key=os.path.getmtime)

    @staticmethod
    def _pump(process, lines):
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    def _send(self, message):
        self._process.stdin.write(json.dumps(message) + '\n')
        self._process.stdin.flush()

    def _request(self, method, params):
        self._next_id += 1
        request_id = self._next_id
        self._send({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
        import time
        deadline=time.monotonic()+_CALL_TIMEOUT
        while True:
            try:
                line = self._lines.get(timeout=max(.001,deadline-time.monotonic()))
            except queue.Empty:
                self._process.terminate(); self._process=None
                raise TimeoutError(f'StudioMCP {method} did not answer within {_CALL_TIMEOUT}s; proxy transport stalled and was reset, not a Studio receiver timeout') from None
            if line is None:
                self._process = None
                raise RuntimeError('StudioMCP exited')
            reply = json.loads(line)
            if reply.get('id') == request_id:
                if 'error' in reply:
                    raise RuntimeError(f"StudioMCP {method} failed: {reply['error']}")
                return reply['result']

    def _ensure_started(self):
        if self._process is not None and self._process.poll() is None:
            return
        self._process = subprocess.Popen([self._executable()], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True, encoding='utf-8', bufsize=1)
        self._lines = queue.Queue()
        threading.Thread(target=self._pump, args=(self._process, self._lines), daemon=True).start()
        self._request('initialize', {'protocolVersion': '2024-11-05', 'capabilities': {},
                                     'clientInfo': {'name': 'studiomcp', 'version': '1'}})
        self._send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})

    def _call(self, name, arguments):
        """Call a StudioMCP tool; caller holds the lock. Returns the content blocks."""
        self._ensure_started()
        result = self._request('tools/call', {'name': name, 'arguments': arguments})
        if result.get('isError'):
            raise RuntimeError(f'StudioMCP {name} failed: ' + ' '.join(b.get('text', '') for b in result['content']))
        return result['content']

    def _studio_id(self, studio):
        """Map a StudioMCP receiver record to its StudioMCP instance id."""
        text = next(b['text'] for b in self._call('list_roblox_studios', {}) if b['type'] == 'text')
        studios = json.loads(text)['studios']
        # An unpublished document has no place identity to compare. Only that
        # case may use the sole proxy; a known mismatched place must fail closed.
        if studio.get('placeId') in (None, 0) and len(studios) == 1:
            return studios[0]['id']
        matches = [s for s in studios if f"placeId: {studio.get('placeId')})" in s['name']]
        if len(matches) == 1:
            return matches[0]['id']
        raise RuntimeError('Cannot match the Studio receiver to one StudioMCP instance: ' + json.dumps(studios))

    def _tool(self, studio, name, arguments=None):
        if not self._lock.acquire(timeout=1):
            raise TimeoutError('StudioMCP proxy is busy with another native operation; status is not waiting behind it')
        try:
            return self._call(name, {'studio_id': self._studio_id(studio), **(arguments or {})})
        finally:
            self._lock.release()

    @staticmethod
    def _text(content):
        return ''.join(b.get('text', '') for b in content if b['type'] == 'text')

    def playtest_status(self, studio):
        """Current Studio mode ('Edit' or 'Play'), available DataModels and the focused one."""
        text = self._text(self._tool(studio, 'get_studio_state'))
        fields = dict(re.findall(r'^- ([^:]+): (.+)$', text, re.M))
        try:
            return {'mode': fields['Current Studio Mode'].strip(),
                    'datamodels': [d.strip() for d in fields['Available DataModels'].split(',')],
                    'focused': fields['Focused DataModel in the viewport'].strip()}
        except KeyError:
            raise RuntimeError('Unrecognised StudioMCP get_studio_state reply: ' + text) from None




    def close(self):
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                self._process.terminate()
            self._process = None


studio_mcp = StudioMCP()
