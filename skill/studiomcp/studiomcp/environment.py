import atexit
import functools
import json
import queue
import threading
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .studio_mcp import studio_mcp
from .errors import classify
from .lease import Events, Lease
from .operations import Operations
from . import targets as target_view
from pathlib import Path
import tempfile


def exclusive(method):
    """Studio has one viewport, camera and Play session; concurrent callers queue instead of interleaving."""
    @functools.wraps(method)
    def locked(self, *args, **kwargs):
        with self.studio_lock:
            return method(self, *args, **kwargs)
    return locked


class Environment:
    def __init__(self, port=23456):
        self.studios = {}
        self.commands = {}
        self.observations = {}
        self.requests = {}
        self.native_receipts = {}
        self.studio_lock = threading.RLock()
        self.lock = threading.RLock()
        self.events = Events()
        self.leases = Lease(self.events)
        self.operations = Operations()
        self.known_receivers = {}
        self.selection_path = Path(tempfile.gettempdir()) / '.StudioMCPConfig'
        self.selected_studio = self.selection_path.read_text(encoding='utf-8').strip() if self.selection_path.is_file() else None
        if sys.platform == 'win32':
            try:
                from .plugin import save_plugin
                save_plugin()
            except Exception:
                pass
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, status, value):
                body = json.dumps(value).encode()
                try:
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def do_GET(self):
                from urllib.parse import urlparse, parse_qs
                parsed = urlparse(self.path)
                if parsed.path == '/health':
                    return self.reply(200, {'service': 'StudioMCP'})
                studio = parse_qs(parsed.query).get('studio', [''])[0]
                if parsed.path != '/commands' or not studio:
                    return self.reply(404, {'error': 'Unknown route'})
                with owner.lock:
                    commands = owner.commands.setdefault(studio, queue.Queue())
                deadline = time.monotonic() + 20
                while True:
                    try:
                        command = commands.get(timeout=max(0, deadline - time.monotonic()))
                    except queue.Empty:
                        return self.reply(200, {'action': 'idle'})
                    with owner.lock:
                        pending = owner.requests.get(command['request'])
                        if pending is not None and time.monotonic() < pending['deadline']:
                            return self.reply(200, command)
                    if time.monotonic() >= deadline:
                        return self.reply(200, {'action': 'idle'})

            def do_POST(self):
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 1024 * 1024:
                        raise ValueError('Invalid request size')
                    body = self.rfile.read(size)
                    if self.path == '/result':
                        request = self.headers.get('X-Request-Id')
                        with owner.lock:
                            pending = owner.requests.get(request)
                            if pending is None:
                                return self.reply(404, {'error': 'Request expired'})
                            pending['chunks'].append(body)
                            if self.headers.get('X-Final') == 'true':
                                pending['ready'].set()
                        return self.reply(200, {'ok': True})
                    data = json.loads(body)
                    if self.path == '/test-result':
                        with owner.lock:
                            observation = owner.observations.get(data['token'])
                            if observation is None:
                                raise ValueError('Unknown test result session')
                            observation['results'] = data['results']
                            observation['ready'].set()
                        return self.reply(200, {'ok': True})
                    if self.path == '/test-observation':
                        import base64
                        from io import BytesIO
                        from PIL import Image
                        with owner.lock:
                            observation = owner.observations.get(data['token'])
                        if observation is None:
                            raise ValueError('Unknown test observation session')
                        if 'png' in data:
                            pixels = base64.b64decode(data['png'], validate=True)
                        else:
                            if not isinstance(data.get('dimensions'),dict):
                                raise RuntimeError('Visual callback lacks current native capture dimensions; reload the StudioMCP plugin; no nested receiver request was queued')
                            pixels = owner.capture_screenshot(observation['studio'], wait_render=False, dimensions=data.get('dimensions'))
                        image = Image.open(BytesIO(pixels)).convert('RGB')
                        observation['images'].append((data['label'], image))
                        return self.reply(200, {'ok': True})
                    if self.path == '/unregister':
                        with owner.lock:
                            owner.studios.pop(data['studio'], None)
                        return self.reply(200, {'ok': True})
                    if self.path == '/register':
                        studio = data['studio']
                        # Never trust provenance posted by a plugin or a manual client.
                        data.pop('nativeProcess',None);data.pop('nativeIdentityError',None)
                        if sys.platform=='win32':
                            from .windows_capture import receiver_process_identity
                            try:
                                data['nativeProcess']=receiver_process_identity(self.connection)
                            except (OSError,RuntimeError) as error:
                                data['nativeIdentityError']=str(error)
                        with owner.lock:
                            previous=owner.studios.get(studio,{})
                            received=time.time()
                            first_render=previous.get('first_render_received',received) if data.get('renderReady') else None
                            owner.studios[studio] = {**data, 'last_seen': received}
                            if first_render is not None:owner.studios[studio]['first_render_received']=first_render
                            owner.commands.setdefault(studio, queue.Queue())
                        return self.reply(200, {'ok': True})
                    if self.path == '/operation':
                        return self.reply(200, owner.dispatch(data['operation'], data.get('arguments', {})))
                    self.reply(404, {'error': 'Unknown route'})
                except Exception as error:
                    self.reply(400, {'error': str(error), 'code': classify(error)})

        self.http = ThreadingHTTPServer(('127.0.0.1', port), Handler, bind_and_activate=False)
        import socket
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.http.allow_reuse_address = False
            self.http.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            self.http.server_bind()
            self.http.server_activate()
        except OSError:
            self.http.server_close()
            raise
        self.http.daemon_threads = True
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        threading.Thread(target=self.watch_receivers, daemon=True).start()
        self.events.record('server', 'StudioMCP server started')
        atexit.register(self.close)

    def studio_at(self, index=None):
        """Receiver id for a 1-based Studio number as shown by `romcp status` (None = the selected Studio)."""
        targets = self.studio_targets()
        if not targets:
            raise ValueError('No current Studio receiver is connected')
        if index is None:
            return self.resolve_studio()
        if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= len(targets):
            raise ValueError(f'Studio number must be between 1 and {len(targets)}')
        return targets[index - 1]['receiver']

    def select_studio(self, studio):
        with self.lock:
            if not any(receiver['studio'] == studio for receiver in self.list_studios()):
                raise ValueError('Studio is not connected')
            self.selection_path.write_text(studio, encoding='utf-8')
            self.selected_studio = studio
            return {'studio': studio}

    def resolve_studio(self, studio=None):
        with self.lock:
            ids = [receiver['studio'] for receiver in self.list_studios()]
            if studio is not None:
                if studio not in ids:
                    raise ValueError('Studio is not connected')
                return studio
            if not ids:
                raise ValueError('No current Studio receiver is connected')
            if len(ids) == 1 or self.selected_studio not in ids:
                self.select_studio(ids[0])
            return self.selected_studio

    def request(self, studio, action, wait_timeout=90, **payload):
        studio = self.resolve_studio(studio)
        desired = payload.get('datamodel', 'auto')
        context = payload.get('context', 'client')
        source_inspection = action in ('readScript', 'grep')
        if action == 'testState':
            desired = 'Edit'
        if action == 'view' or source_inspection:
            context = 'server'
        edit_actions = {'camera', 'manifest', 'pull', 'push', 'claim', 'ownership', 'deleteOwned', 'validatePush', 'test', 'testPrepare', 'testCleanup', 'libraryAdd', 'insert', 'editScript', 'transportChunk', 'transportValidate', 'transportApply', 'transportAbort'}
        if action in edit_actions:
            desired = 'Edit'
        with self.lock:
            selected = self.studios[studio]
            candidates = [receiver for receiver in self.list_studios() if receiver.get('placeId') == selected.get('placeId') and receiver.get('userId') == selected.get('userId')]
            if desired != 'Edit':
                live = [receiver for receiver in candidates if receiver.get('datamodel') == 'Play' and receiver.get('context') == context]
                if any(receiver['studio'] == studio for receiver in live):
                    studio = selected['studio']
                elif len(live) == 1:
                    studio = live[0]['studio']
                elif live:
                    raise ValueError(f'Multiple live Play {context} receivers match this place; select the intended receiver explicitly; no command was queued')
                elif desired == 'Play' or (source_inspection and any(receiver.get('datamodel') == 'Play' for receiver in candidates)):
                    raise ValueError(f'No live Play {context} receiver is connected')
            else:
                edit = [receiver for receiver in candidates if receiver.get('datamodel', 'Edit') == 'Edit']
                if not edit:
                    raise ValueError('No live Edit receiver is connected; no command was queued and the current Play session was left intact')
                if any(receiver['studio'] == studio for receiver in edit):
                    studio = selected['studio']
                elif len(edit) == 1:
                    studio = edit[0]['studio']
                else:
                    raise ValueError('Multiple live Edit receivers match this place; select the intended Edit receiver explicitly; no command was queued')
        request_id = uuid.uuid4().hex
        pending = {'chunks': [], 'ready': threading.Event(), 'deadline': time.monotonic() + wait_timeout}
        with self.lock:
            if studio not in self.studios:
                raise ValueError('Studio is not registered')
            self.requests[request_id] = pending
            self.commands[studio].put({'action': action, 'request': request_id, **payload})
        try:
            if not pending['ready'].wait(wait_timeout):
                raise TimeoutError('Studio did not complete ' + action)
            result = json.loads(b''.join(pending['chunks']))
            if 'error' in result:
                raise RuntimeError(result['error'])
            return result
        finally:
            with self.lock:
                self.requests.pop(request_id, None)
                commands = self.commands[studio]
                with commands.mutex:
                    commands.queue = type(commands.queue)(command for command in commands.queue if command['request'] != request_id)

    def list_studios(self):
        with self.lock:
            receivers = [receiver for receiver in self.studios.values() if time.time() - receiver['last_seen'] < 45]
            installed = [receiver for receiver in receivers if receiver.get('installed')]
            return [dict(receiver, index=index) for index, receiver in enumerate(installed or receivers, 1)]

    def output(self, studio):
        return self.request(studio, 'output', root='game')

    def camera(self, action='status', studio=None):
        if action not in ('status', 'reset'):
            raise ValueError('Camera action must be status or reset')
        return self.request(studio, 'camera', root='game', mode=action)

    def ls(self, path='game', recursive=False, limit=200, cursor=None, studio=None):
        return self.request(studio, 'ls', root='game', path=path, recursive=recursive, limit=limit, cursor=cursor)

    def find(self, path='game', name=None, className=None, limit=200, cursor=None, studio=None):
        return self.request(studio, 'find', root='game', path=path, name=name, className=className, limit=limit, cursor=cursor)

    def cat(self, path, studio=None):
        return self.request(studio, 'cat', root='game', path=path)

    def coverage(self, root='game', studio=None):
        return self.request(studio, 'coverage', wait_timeout=600, root=root)

    def view(self, studio, path, output=None, rig=None, angle=None, samples=9, appearance='avatar', rig_type=None, camera_cframe=None, contact_sheet=False):
        import base64
        studio = self.resolve_studio(studio)
        if path == 'game' and not any((rig, angle, camera_cframe, contact_sheet)):
            png=self.capture_screenshot(studio)
            if output is not None:
                target=Path(output).resolve(); target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(png)
                return {'path':str(target),'transport':'WindowsVisibleViewport'}
            return {'png':base64.b64encode(png).decode('ascii'),'transport':'WindowsVisibleViewport'}
        return self._inspect_view(studio,path,output,rig,angle,samples,appearance,rig_type,camera_cframe,contact_sheet)

    @exclusive
    def _inspect_view(self, studio, path, output, rig, angle, samples, appearance, rig_type, camera_cframe, contact_sheet):
        import base64
        from io import BytesIO
        from PIL import Image
        from pathlib import Path
        from .gallery import gallery
        description = self.request(studio, 'ls', root='game', path=path, limit=1)
        if description['root']['className'] == 'KeyframeSequence':
            raise ValueError('Viewing KeyframeSequence is supported headlessly via RbxMCP2')
        # Inspection camera/state ownership is separate from the isolated capture worker.
        images = []
        try:
            session = self.request(studio, 'view', root='game', path=path, stage='begin', rig=rig, angle=angle, samples=samples, camera_cframe=camera_cframe, contact_sheet=contact_sheet)
            for index in range(1, session['views'] + 1):
                view = self.request(studio, 'view', root='game', path=path, stage='view', index=index, angle=angle, camera_cframe=camera_cframe)
                png_bytes = self.capture_screenshot(studio)
                images.append((view['label'], Image.open(BytesIO(png_bytes)).convert('RGB')))
        finally:
            self.request(studio, 'view', root='game', path=path, stage='end')
        image = gallery(images)
        if output is not None:
            target = Path(output).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(image)
            return {'path': str(target)}
        return {'png': base64.b64encode(image).decode('ascii')}

    def capture_screenshot(self, studio, wait_render=True, dimensions=None):
        from .windows_capture import awake_viewport, capture_bounded, viewport_dimensions
        record=self.studios[studio]
        if dimensions is None:
            viewport,simulator,play=viewport_dimensions(self,studio)
        else:
            # Visual HTTP callbacks carry freshly measured native metadata;
            # querying their busy receiver would deadlock behind this response.
            viewport=dimensions.get('viewport')
            simulator=dimensions.get('simulator') or None
            play=True
        with awake_viewport(record,viewport,simulator):
            if wait_render and play:
                probe=self.request(studio,'execute',datamodel='Play',timeout=2,wait_timeout=3,code='local n=0;local c=game:GetService("RunService").RenderStepped:Connect(function() n+=1 end);local deadline=os.clock()+1.8;repeat task.wait(.02) until n>=2 or os.clock()>=deadline;c:Disconnect();local v=workspace.CurrentCamera.ViewportSize;return n,v.X,v.Y')
                if not probe.get('success') or len(probe.get('results',[]))!=3 or probe['results'][0]<2:
                    raise RuntimeError('Studio was foregrounded but no fresh client render frames arrived within 2s; refusing stale capture')
                viewport=probe['results'][1:]
            return capture_bounded(record,viewport=viewport,simulator=simulator)

    def play_mode(self, record):
        """Edit/Play for one Studio. Asks the official StudioMCP first; when that bridge cannot see this Studio
        (its MCP server setting is off, or it lists no Studios) fall back to the RoMCP plugin's own receivers,
        which already know whether a Play DataModel is connected."""
        try:
            return studio_mcp.playtest_status(record)
        except RuntimeError as error:
            if 'StudioMCP' not in str(error):
                raise
        with self.lock:
            playing = any(r.get('placeId') == record.get('placeId') and r.get('userId') == record.get('userId')
                          and r.get('datamodel') == 'Play' and time.time() - r.get('last_seen', 0) < 12
                          for r in self.studios.values())
        return {'mode': 'Play' if playing else 'Edit', 'datamodels': [], 'focused': '', 'source': 'plugin'}

    def playtest(self, action, studio=None):
        if action == 'status':
            return self.play_mode(self.studios[self.resolve_studio(studio)])
        return self._playtest_transition(action, studio)

    @exclusive
    def _playtest_transition(self, action, studio=None, owner=None):
        """Send a visible native Play/Stop shortcut once, then observe readiness."""
        studio = self.resolve_studio(studio)
        record = self.studios[studio]
        if action == 'status':
            return self.play_mode(record)
        if action not in ('start', 'stop'):
            raise ValueError("playtest action must be 'status', 'start' or 'stop'")
        wanted = 'Play' if action == 'start' else 'Edit'
        status = self.play_mode(record)
        if status['mode'] == wanted:
            raise RuntimeError(f'Studio is already in {wanted}; nothing to {action}')
        started_at=time.time()
        started_clock=time.monotonic()
        if action == 'start':
            from .windows_play import start
            start(record)
            # The native start shortcut has been sent. Protect this caller's
            # session even if subsequent foreground/readiness observation fails.
            if owner is not None:self.leases.note_play(True, owner)
        else:
            from .windows_play import stop
            stop(record)
        shortcut = 'F5' if action == 'start' else 'Shift+F5'
        self.events.record('play-request', f'{owner or "direct caller"} requested {wanted} via native {shortcut}',
                           receiver=studio, action=action, shortcut=shortcut)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if action == 'start':
                from .windows_capture import NativeWindowNotResponsive, focus
                try:
                    focus(record)
                except NativeWindowNotResponsive:
                    # Loading may briefly hang the native window after F5.
                    # Defer readiness only within this transition's deadline.
                    pass
                else:
                    with self.lock:
                        clients=[r for r in self.studios.values() if r.get('placeId')==record.get('placeId') and r.get('userId')==record.get('userId') and r.get('datamodel')=='Play' and r.get('context')=='client' and r.get('renderReady') and r.get('first_render_received',0)>=started_at]
                    if clients:
                        return {'mode':'Play','viewport':clients[0]['viewport'],'readiness':'firstRenderStepped','readySeconds':round(time.monotonic()-started_clock,3),'firstRenderReceivedSeconds':round(clients[0]['first_render_received']-started_at,3),'renderGameTime':clients[0].get('renderGameTime')}
            else:
                status = self.play_mode(record)
                if status['mode'] == wanted:
                    return status
            time.sleep(0.25)
        observed_at = time.time()
        elapsed = round(time.monotonic() - started_clock, 3)
        with self.lock:
            contexts = []
            for receiver in self.studios.values():
                if receiver.get('placeId') != record.get('placeId') or receiver.get('userId') != record.get('userId'):
                    continue
                seen = receiver.get('last_seen')
                age = round(observed_at - seen, 3) if seen is not None else None
                contexts.append({'receiver': receiver.get('studio'), 'datamodel': receiver.get('datamodel'),
                                 'context': receiver.get('context'), 'lastSeenAgeSeconds': age,
                                 'live': seen is not None and observed_at - seen < 45, 'renderReady': receiver.get('renderReady', False),
                                 'viewport': receiver.get('viewport'), 'firstRenderReceived': receiver.get('first_render_received')})
        status_phase = 'before shortcut' if action == 'start' else 'after shortcut'
        self.events.record('play-timeout', f'{wanted} transition was not confirmed after native {shortcut}',
                           receiver=studio, action=action, elapsedSeconds=elapsed, nativeStatus=status,
                           nativeStatusPhase=status_phase, receivers=contexts)
        readiness = 'rendered Play' if action == 'start' else 'native Edit'
        raise TimeoutError(f'{action} was requested once but Studio did not confirm {readiness} within 40s '
                           f'(elapsed {elapsed}s); last queried native status ({status_phase}): {status}; observed receivers: {contexts}; '
                           'renderReady records the first client frame, not current rendering or native mode; '
                           'no automatic retry was sent and Play ownership was preserved; inspect Studio before retrying')

    def run_server(self, code, timeout=30, studio=None):
        """Run Luau headlessly on the server in Run mode (StudioTestService); no Studio window required."""
        return self.request(studio, 'runServer', code=code, timeout=timeout, wait_timeout=timeout + 60)

    def grep(self, query, plain=True, ignore_case=False, limit=200, skip=0, studio=None):
        """Search every script's source; follow `next` while `truncated`. plain=False takes Luau patterns."""
        return self.request(studio, 'grep', root='game', query=query, plain=plain, ignore_case=ignore_case, limit=limit, skip=skip)

    def save_plugin(self, directory):
        from .plugin import save_plugin
        result = save_plugin(directory)
        result['outputs'] = []
        for studio in self.list_studios():
            if studio.get('protocol') == 2 and time.time() - studio['last_seen'] < 45:
                try:
                    result['outputs'].append({'studio': studio['studio'], **self.output(studio['studio'])})
                except (RuntimeError, TimeoutError) as error:
                    result['outputs'].append({'studio': studio['studio'], 'error': str(error)})
        return result

    def library_options(self):
        from .library import options
        return options()

    def library_search(self, query='', category='Model', filters=None, output=None):
        from .library import search
        return search(query, category, filters, output)

    def library_add(self, assetId, name=None, studio=None):
        from .library import details
        metadata = details(assetId)
        asset = metadata['asset']
        product = metadata.get('creatorStoreProduct') or {}
        if product.get('pluginAssetId') or 'plugin' in asset:
            raise ValueError('Plugin discovery only; automatic installation is prohibited')
        return self.request(studio, 'libraryAdd', root='game', assetId=str(asset['id']), name=name or asset['name'])

    def export_asset(self, path, output=None, studio=None):
        import base64
        from pathlib import Path
        result = self.request(studio, 'exportAsset', root='game', path=path)
        if not output:
            output = f"{result['name']}.rbxm"
        destination = Path(output).resolve()
        if destination.suffix.lower() != '.rbxm':
            destination = destination.with_suffix('.rbxm')
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(base64.b64decode(result['data'], validate=True))
        return {'output': str(destination), 'name': result['name'], 'assetType': result.get('assetType')}

    def upload(self, file, creator_id=None, creator_type='user', asset_type=None, name=None, description='', wait_seconds=60, studio=None):
        from .upload import upload
        if creator_id is None:
            target = self.resolve_studio(studio)
            creator_id = self.studios.get(target, {}).get('userId')
        if not file.startswith(('game.', 'game/')):
            return upload(file, creator_id, creator_type, asset_type, name, description, wait_seconds)
        import base64
        import tempfile
        from pathlib import Path
        result = self.request(studio, 'exportAsset', root='game', path=file)
        with tempfile.TemporaryDirectory(prefix='studiomcp-upload-') as directory:
            path = Path(directory) / 'asset.rbxm'
            path.write_bytes(base64.b64decode(result['data'], validate=True))
            return upload(str(path), creator_id, creator_type, asset_type or result['assetType'], name or result['name'], description, wait_seconds)

    # ---- who is connected --------------------------------------------------------------
    def studio_targets(self):
        """Connected Studios as stable targets with named contexts (Edit, Play/server, Play/client)."""
        return target_view.group(self.list_studios())

    def watch_receivers(self):
        """Log receivers appearing and disappearing, so 'who ended my Play session?' has an answer."""
        while True:
            time.sleep(3)
            try:
                with self.lock:
                    now = {r['studio']: target_view.context_name(r) for r in self.studios.values() if time.time() - r['last_seen'] < 45}
                for key, name in now.items():
                    if key not in self.known_receivers:
                        self.events.record('receiver', f'{name} connected', receiver=key)
                for key, name in self.known_receivers.items():
                    if key not in now:
                        self.events.record('receiver', f'{name} disconnected', receiver=key)
                self.known_receivers = now
            except Exception:
                pass  # a monitoring hiccup must never take the server down

    def wait_for_studio(self, seconds=20):
        """Studio plugins reconnect within seconds after a server restart; wait instead of failing."""
        deadline = time.monotonic() + seconds
        while not self.list_studios() and time.monotonic() < deadline:
            time.sleep(0.5)

    def status(self):
        running = [op for op in self.operations.listing() if op['state'] == 'running']
        targets = self.studio_targets()
        self.leases.reconcile(targets)
        return {'studios': targets, 'lock': self.leases.status(), 'running': running, 'recentEvents': self.events.recent(8)}

    def output_summary(self, studio, limit=5):
        """Errors and warnings in the Studio output, condensed to the few distinct messages that matter."""
        try:
            entries = self.output(studio).get('entries', [])
        except (RuntimeError, TimeoutError, ValueError) as error:
            return {'unavailable': str(error)}
        counts = {}
        for entry in entries:
            kind = str(entry.get('type', '')).rsplit('.', 1)[-1].replace('Message', '')
            if kind in ('Error', 'Warning'):
                key = (kind, entry.get('message', '')[:300])
                counts[key] = counts.get(key, 0) + 1
        ordered = sorted(counts.items(), key=lambda item: (item[0][0] != 'Error', -item[1]))
        return {'errors': sum(n for (kind, _), n in counts.items() if kind == 'Error'),
                'warnings': sum(n for (kind, _), n in counts.items() if kind == 'Warning'),
                'distinct': [{'type': kind, 'count': n, 'message': message} for (kind, message), n in ordered[:limit]]}

    # ---- composed workflows ------------------------------------------------------------
    def playtest_with_output(self, action, studio, owner):
        """Start/stop Play and report what the console said, so no separate `output` call is needed."""
        studio = self.resolve_studio(studio)
        if action == 'stop':
            summary = self.output_summary(studio)
            result = self.playtest('stop', studio)
            self.leases.note_play(False, owner)
            self.events.record('play', f'{owner} stopped Play')
            return {**result, 'output': summary}
        result = self._playtest_transition('start', studio, owner)
        self.events.record('play', f'{owner} started Play')
        time.sleep(2.5)  # let scripts run their first frames and report startup errors
        return {**result, 'output': self.output_summary(studio)}

    def guarded_execute(self, arguments):
        """Run Luau, then undo any camera/selection change it caused and say so."""
        from . import guard
        from .tools import invoke
        studio = self.studio_at(arguments.get('studio'))
        rest = {k: v for k, v in arguments.items() if k != 'studio'}
        if arguments.get('context', 'client') == 'server':
            return invoke(self, 'execute', studio, **rest)
        place = {key: arguments[key] for key in ('datamodel', 'context') if key in arguments}
        def probe(code):
            reply = invoke(self, 'execute', studio, code=code, timeout=10, **place)
            return (reply.get('results') or [None])[0] if reply.get('success') else None
        before = probe(guard.PROBE)
        in_edit = bool(before and before.get('inEdit'))
        result = invoke(self, 'execute', studio, **rest)
        changed = guard.differences(before, probe(guard.PROBE), in_edit)
        if changed and before:
            probe(guard.restore_code(before, in_edit))
            result = {**result, 'sideEffects': changed, 'restored': True,
                      'note': 'execute is observational by default; pass --allowSideEffects if you meant to change the camera or selection.'}
        return result

    def profile(self, modules, seconds=5, only=None, context='client', studio=None):
        """Per-function call counts and inclusive/self time for chosen modules over `seconds` of Play."""
        from . import profiler
        seconds = max(1, min(float(seconds), 45))
        build = profiler.server_script if context == 'server' else profiler.script
        reply = self.request(studio, 'execute', datamodel='Play', context=context, timeout=seconds + 10, wait_timeout=seconds + 30,
                             code=build(modules, seconds, only))
        if not reply.get('success') or not reply.get('results'):
            raise RuntimeError(reply.get('message') or 'profiling failed; is Play running?')
        result = profiler.listify(reply['results'][0])
        for key in ('rows', 'missing'):
            if not result.get(key):
                result[key] = []
        if 'error' in result:
            raise RuntimeError(result['error'])
        result['text'] = profiler.render(result)
        return result

    @exclusive
    def capture(self, studio, frames=6, interval_ms=500, state=None, context='client', output=None):
        """A bounded, timestamped screenshot sequence, each frame paired with an optional runtime sample.

        `state` is a Luau expression (e.g. "workspace.Actors:GetChildren()[1].Humanoid.WalkSpeed")
        evaluated in `context` right after each frame, which ties the pictures to game state.
        """
        from io import BytesIO
        from PIL import Image
        from .gallery import gallery
        studio = self.resolve_studio(studio)
        frames, interval_ms = int(frames), max(0, int(interval_ms))
        if not 1 <= frames <= 60 or frames * max(interval_ms, 500) > 120_000:
            raise ValueError('capture is bounded: 1-60 frames and about two minutes in total')
        folder = Path(output or Path('.screenshots') / time.strftime('sequence-%H%M%S')).resolve()
        folder.mkdir(parents=True, exist_ok=True)
        started, entries, images = time.monotonic(), [], []
        for index in range(frames):
            begin = time.monotonic()
            png = self.capture_screenshot(studio, wait_render=index == 0)
            moment = time.monotonic() - started
            sample = None
            if state:
                code = state if state.lstrip().startswith('return') else 'return ' + state
                reply = self.request(studio, 'execute', datamodel='Play', context=context, timeout=5, wait_timeout=8, code=code)
                sample = (reply.get('results') or [reply.get('message')])[0]
            path = folder / f'{index:02d}-{int(moment * 1000):06d}ms.png'
            path.write_bytes(png)
            entries.append({'index': index, 'ms': int(moment * 1000), 'file': str(path), 'state': sample})
            if frames <= 12:
                images.append((f'{int(moment * 1000)} ms', Image.open(BytesIO(png)).convert('RGB')))
            time.sleep(max(0, interval_ms / 1000 - (time.monotonic() - begin)))
        result = {'directory': str(folder), 'frames': entries}
        if images:
            sheet = folder / 'sheet.png'
            sheet.write_bytes(gallery(images))
            result['sheet'] = str(sheet)
        return result

    # ---- the single entry point for every request -------------------------------------
    LOCAL_OPERATIONS = {'status', 'studios', 'receivers', 'operation', 'operations', 'lock', 'events', 'selectStudio'}
    NO_STUDIO_NEEDED = {'libraryOptions', 'librarySearch', 'uploadStatus', 'createGamePass', 'createDeveloperProduct',
                        'publishModel', 'httpGet', 'savePlugin'}

    def dispatch(self, operation, arguments):
        """Reserve pending Studio work atomically; workers release protection on completion.
        Slow operations return a receipt while their activity protection remains held."""
        arguments = dict(arguments)
        owner = arguments.pop('owner', None) or 'anonymous'
        force = arguments.pop('force', False)
        if operation in self.LOCAL_OPERATIONS:
            return self.local_operation(operation, arguments, owner, force)
        action = arguments.get('action')
        key = f'playtest-{action}' if operation == 'playtest' and action in ('start', 'stop') else operation
        self.leases.reconcile(self.studio_targets())
        if key == 'playtest-stop':
            self.leases.check_stop(owner, force)
        local_upload = operation == 'upload' and not str(arguments.get('file', '')).startswith(('game.', 'game/'))
        uses_studio = operation not in self.NO_STUDIO_NEEDED and not local_upload or operation == 'savePlugin'
        token = self.leases.begin(key, owner, force) if uses_studio else None
        def work():
            try:
                if uses_studio and operation != 'savePlugin':
                    self.wait_for_studio()
                return self.perform(operation, arguments, owner)
            finally:
                self.leases.finish(token)
        try:
            return self.operations.run(operation, work, wait=60 if operation == 'playtest' else None)
        except BaseException:
            self.leases.finish(token)
            raise

    def local_operation(self, operation, arguments, owner, force):
        if operation == 'status':
            return self.status()
        if operation == 'studios':
            return self.studio_targets()
        if operation == 'receivers':
            return self.list_studios()
        if operation == 'selectStudio':
            return self.select_studio(self.studio_at(arguments['studio']))
        if operation == 'operation':
            return self.operations.get(arguments['id'], wait=min(float(arguments.get('wait') or 0), 300))
        if operation == 'operations':
            return self.operations.listing()
        if operation == 'events':
            return self.events.recent(int(arguments.get('limit', 40)))
        if arguments.get('action', 'status') != 'status':
            raise ValueError('Studio protection is automatic; manual lock acquisition/release is not supported.')
        self.leases.reconcile(self.studio_targets())
        return self.leases.status()

    def perform(self, operation, arguments, owner):
        from .upload import operation as upload_status
        from .monetization import create_game_pass, create_developer_product
        from .store import publish_model
        from .tools import invoke
        allow_side_effects = arguments.pop('allowSideEffects', False)
        if operation in ('execute', 'keyboard', 'mouse', 'navigate', 'readScript', 'editScript', 'searchScripts', 'insertAsset', 'httpGet'):
            arguments = dict(arguments)
            index = arguments.pop('studio', None)
            studio = None if operation == 'httpGet' else self.studio_at(index)
            if operation == 'execute' and not allow_side_effects:
                return self.guarded_execute({**arguments, 'studio': index})
            return invoke(self, operation, studio, **arguments)
        if operation == 'playtest' and arguments.get('action') in ('start', 'stop'):
            index = arguments.get('studio')
            return self.playtest_with_output(arguments['action'], self.studio_at(index) if index else None, owner)
        if operation == 'profile':
            return self.profile(arguments['modules'], arguments.get('seconds', 5), arguments.get('only'),
                                arguments.get('context', 'client'), self.studio_at(arguments.get('studio')))
        if operation == 'capture':
            return self.capture(self.studio_at(arguments.get('studio')), arguments.get('frames', 6), arguments.get('interval', 500),
                                arguments.get('state'), arguments.get('context', 'client'), arguments.get('output'))
        methods = {'output': self.output, 'savePlugin': self.save_plugin, 'view': self.view,
                   'ls': self.ls, 'cat': self.cat, 'find': self.find,
                   'playtest': self.playtest, 'camera': self.camera, 'grep': self.grep, 'runServer': self.run_server, 'coverage': self.coverage,
                   'libraryOptions': self.library_options, 'librarySearch': self.library_search, 'libraryAdd': self.library_add,
                   'upload': self.upload, 'uploadStatus': upload_status,
                   'createGamePass': create_game_pass, 'createDeveloperProduct': create_developer_product,
                   'exportAsset': self.export_asset, 'publishModel': publish_model}
        if operation not in methods:
            raise ValueError('Unknown operation: ' + operation)
        import inspect
        if 'studio' in inspect.signature(methods[operation]).parameters:
            arguments = dict(arguments, studio=self.studio_at(arguments.get('studio')))  # None means the selected Studio
        result = methods[operation](**arguments)
        return result

    def close(self):
        studio_mcp.close()
        self.http.shutdown()
        self.http.server_close()
        atexit.unregister(self.close)
