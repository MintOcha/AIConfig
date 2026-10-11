"""Safe receiver-backed state tools shared by CLI and FastMCP."""
import json


def configure_cli(sub):
    execute = sub.add_parser('execute', help='Run Luau without touching camera, selection or viewport')
    execute.add_argument('code')
    execute.add_argument('--datamodel', choices=('auto', 'Edit', 'Play'), default='auto')
    execute.add_argument('--context', choices=('client', 'server'), default='client')
    execute.add_argument('--timeout', type=float, default=30)
    execute.add_argument('--allowSideEffects', action='store_true', help='skip the camera/selection safety net (execute normally undoes such changes)')
    read = sub.add_parser('readScript')
    read.add_argument('path')
    read.add_argument('--start', type=int, default=1)
    read.add_argument('--finish', type=int, default=None)
    edit = sub.add_parser('editScript')
    edit.add_argument('path')
    edit.add_argument('edits', type=json.loads, help='JSON [{start,finish,text}]; finish=start-1 inserts, empty text deletes')
    edit.add_argument('--expected', default=None, help='Expected complete source; reject concurrent changes')
    edit.add_argument('--className', choices=('Script', 'LocalScript', 'ModuleScript'), default=None, help='Create missing script with this native class')
    search = sub.add_parser('searchScripts')
    search.add_argument('name', nargs='?', default=None)
    search.add_argument('--limit', type=int, default=200)
    search.add_argument('--cursor', default=None)
    for name in ('keyboard', 'mouse'):
        parser = sub.add_parser(name)
        parser.add_argument('actions', type=json.loads)
    navigate = sub.add_parser('navigate', help='Walk an actual Humanoid along native pathfinding waypoints in Play')
    navigate.add_argument('target', nargs='?', default=None, help='destination: instance path (e.g. Workspace.Part) or JSON [x,y,z]')
    navigate.add_argument('--path', default=None, help='destination instance path')
    navigate.add_argument('--position', type=json.loads, default=None, help='destination coordinates JSON [x,y,z]')
    navigate.add_argument('--character', default=None, help='character model (default: local player character)')
    navigate.add_argument('--timeout', type=float, default=30)
    insert = sub.add_parser('insertAsset')
    insert.add_argument('assetId')
    insert.add_argument('--name', default=None)
    http = sub.add_parser('httpGet')
    http.add_argument('url')
    for parser in (execute, read, edit, search, navigate, insert, http):
        parser.add_argument('--studio', type=int, default=None)
    # The two input parsers are retrieved above without duplicating their setup.
    for name in ('keyboard', 'mouse'):
        sub.choices[name].add_argument('--studio', type=int, default=None)


def invoke(environment, operation, studio=None, **arguments):
    if operation == 'searchScripts':
        return environment.find('game', arguments.get('name'), 'LuaSourceContainer', arguments.get('limit', 200), arguments.get('cursor'), studio)
    actions = {'httpGet': 'http', 'insertAsset': 'insert', 'readScript': 'readScript', 'editScript': 'editScript',
               'execute': 'execute', 'keyboard': 'keyboard', 'mouse': 'mouse', 'navigate': 'navigate'}
    if operation in ('keyboard', 'mouse'):
        from .windows_input import send
        return send(environment, studio, operation, arguments['actions'])
    if operation == 'execute':
        timeout = arguments.setdefault('timeout', 30)
        if not 0 < timeout <= 60:
            raise ValueError('timeout must be greater than zero and at most 60 seconds')
    if operation == 'httpGet':
        from urllib.parse import urlparse
        parsed = urlparse(arguments['url'])
        if parsed.scheme != 'https' or parsed.hostname not in ('create.roblox.com', 'github.com') or not parsed.path.endswith(('.md', '/llms.txt')):
            raise ValueError('httpGet accepts HTTPS Roblox documentation or GitHub Markdown/llms.txt URLs only')
        # Roblox rejects HttpService calls to its documentation host. Fetch public
        # docs locally instead; no cookie/auth or Studio-state tool is involved.
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        request = Request(arguments['url'], headers={'User-Agent': 'StudioMCP/1'})
        try:
            response = urlopen(request, timeout=30)
        except HTTPError as error:
            response = error
        with response:
            return {'success': 200 <= response.status < 300, 'status': response.status,
                    'body': response.read().decode('utf-8'), 'headers': dict(response.headers)}
    if operation in ('keyboard', 'mouse'):
        valid = {'keyboard': {'keyDown', 'keyUp', 'keyPress', 'textInput', 'wait'},
                 'mouse': {'moveTo', 'mouseButtonDown', 'mouseButtonUp', 'mouseButtonClick', 'scrollUp', 'scrollDown', 'wait'}}[operation]
        for action in arguments['actions']:
            if action.get('action') not in valid:
                raise ValueError('Unknown input action')
            if action['action'] == 'wait' and not 0 <= action.get('waitTimeMs', -1) <= 10000:
                raise ValueError('waitTimeMs must be between 0 and 10000')
            if operation == 'keyboard' and action['action'] in ('keyDown', 'keyUp', 'keyPress') and not action.get('keyCode'):
                raise ValueError('Key actions require keyCode')
            if action['action'] == 'textInput' and not isinstance(action.get('textInputs'), str):
                raise ValueError('textInput requires textInputs')
            if action['action'] in ('mouseButtonDown', 'mouseButtonUp', 'mouseButtonClick') and action.get('mouseButton') not in ('left', 'right'):
                raise ValueError('Mouse buttons must be left or right')
    if operation == 'navigate':
        target = arguments.pop('target', None)
        path = arguments.get('path')
        position = arguments.get('position')
        if target and not path and not position:
            if isinstance(target, (list, tuple)):
                position = list(target)
            elif isinstance(target, str):
                target_str = target.strip()
                if target_str.startswith('[') and target_str.endswith(']'):
                    try:
                        position = json.loads(target_str)
                    except Exception:
                        path = target_str
                elif ',' in target_str and not target_str.startswith('game'):
                    try:
                        position = [float(x.strip()) for x in target_str.split(',')]
                    except Exception:
                        path = target_str
                else:
                    path = target_str
            arguments['path'] = path
            arguments['position'] = position
        if not arguments.get('path') and not arguments.get('position'):
            raise ValueError('Specify a destination target (instance path or [x,y,z] coordinates)')
        position = arguments.get('position')
        if position is not None and (len(position) != 3 or any(not isinstance(value, (int, float)) for value in position)):
            raise ValueError('position must be [x,y,z]')
    if operation == 'editScript':
        if not arguments.get('className'):
            path_str = str(arguments.get('path', '')).lower()
            if 'starterplayer' in path_str or 'startergui' in path_str:
                arguments['className'] = 'LocalScript'
            elif 'serverscriptservice' in path_str:
                arguments['className'] = 'Script'
            else:
                arguments['className'] = 'ModuleScript'
    return environment.request(studio, actions[operation], root='game', wait_timeout=arguments.get('timeout', 60) + 15, **arguments)


def register_mcp(server, environment):
    import asyncio
    async def call(operation, studio=None, **arguments):
        target = None if operation == 'httpGet' else environment.studio_at(studio)
        return await asyncio.to_thread(invoke, environment, operation, target, **arguments)

    @server.tool()
    async def execute(code: str, datamodel: str = 'auto', context: str = 'client', timeout: float = 30, studio: int | None = None) -> dict:
        """Run trusted Luau in active Edit/Play model; return values, errors and captured print/warn. No camera/selection changes or rollback."""
        return await call('execute', studio, code=code, datamodel=datamodel, context=context, timeout=timeout)

    @server.tool()
    async def readScript(path: str, start: int = 1, finish: int | None = None, studio: int | None = None) -> dict:
        """Read editor source with numbered lines and total count."""
        return await call('readScript', studio, path=path, start=start, finish=finish)

    @server.tool()
    async def editScript(path: str, edits: list[dict], expected: str | None = None, className: str | None = None, studio: int | None = None) -> dict:
        """Apply non-overlapping original line ranges atomically; expected checks complete prior source."""
        return await call('editScript', studio, path=path, edits=edits, expected=expected, className=className)

    @server.tool()
    async def searchScripts(name: str | None = None, limit: int = 200, cursor: str | None = None, studio: int | None = None) -> dict:
        """Search script names by substring, with hierarchy cursors; follow until complete."""
        return await call('searchScripts', studio, name=name, limit=limit, cursor=cursor)

    @server.tool()
    async def keyboard(actions: list[dict], studio: int | None = None) -> dict:
        """Send ordered Play keyboard keyDown/keyUp/keyPress/textInput/wait actions."""
        return await call('keyboard', studio, actions=actions)

    @server.tool()
    async def mouse(actions: list[dict], studio: int | None = None) -> dict:
        """Send ordered Play mouse actions; GuiObject paths resolve to centers."""
        return await call('mouse', studio, actions=actions)

    @server.tool()
    async def navigate(target: str | list[float] | None = None, path: str | None = None, position: list[float] | None = None, character: str | None = None, timeout: float = 30, studio: int | None = None) -> dict:
        """Walk an actual Humanoid along native pathfinding waypoints in Play. Provide target (path or [x,y,z]) or explicit path/position."""
        return await call('navigate', studio, target=target, path=path, position=position, character=character, timeout=timeout)

    @server.tool()
    async def insertAsset(assetId: str, name: str | None = None, studio: int | None = None) -> dict:
        """Load native asset in Edit to isolated ServerStorage discovery folder with scripts disabled; never execute imports."""
        return await call('insertAsset', studio, assetId=assetId, name=name)

    @server.tool()
    async def httpGet(url: str, studio: int | None = None) -> dict:
        """GET public documentation locally (Roblox blocks receiver HttpService); return status, headers and complete body."""
        return await call('httpGet', studio, url=url)
