"""The `studiomcp` command line.

Commands talk to one background server (started automatically if it is down),
Studio numbers are inferred where possible, output is short human text (`--json`
gives the raw reply), and exit codes signal status:
0 ok, 1 failed, 2 bad usage/setup, 3 Studio unavailable or busy, 4 timed out / still running.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import report
from .errors import EXIT_FAILED, EXIT_OK, EXIT_TIMEOUT, EXIT_UNAVAILABLE, EXIT_USAGE, exit_code

DEFAULT_PORT = 23456
EPILOG = """exit codes: 0 ok | 1 failed | 2 bad usage or setup | 3 Studio unavailable or busy (retry) | 4 timed out / running in background

commands: status | playtest start|stop | view | execute | output | ls | cat | find | runServer | grep
long jobs: anything over 30 s returns an operation id; fetch it with `studiomcp operation ID --wait`
"""




def build_parser():
    parser = argparse.ArgumentParser(prog='studiomcp', description='Control Roblox Studio directly via MCP and CLI.', epilog=EPILOG,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    parser.add_argument('--json', action='store_true', help='print the raw JSON reply')
    sub = parser.add_subparsers(dest='command', required=True, metavar='command')

    def command(name, help_text, *aliases):
        return sub.add_parser(name, help=help_text, aliases=list(aliases))

    def studio_option(p):
        p.add_argument('--studio', type=int, default=None, help='Studio number from `studiomcp status`')

    serve = command('serve', 'run the background server')
    serve.add_argument('--mcp', choices=['stdio', 'http'])
    serve.add_argument('--mcpPort', dest='mcp_port', type=int, default=23457)

    command('status', 'what is connected, who holds the lock, what is running', 'studios')
    command('events', 'recent happenings in Studio and receiver').add_argument('--limit', type=int, default=30)
    operation = command('operation', 'fetch the result of a long-running operation')
    operation.add_argument('id')
    operation.add_argument('--wait', type=float, nargs='?', const=300, default=0, help='wait up to N seconds for it to finish')
    command('operations', 'list recent operations')
    command('lock', 'show automatic Studio activity protection')
    select = command('selectStudio', 'choose the default Studio')
    select.add_argument('studio', type=int)

    # Play and verification
    play = command('playtest', 'start/stop Play; start and stop report console errors', 'play')
    play.add_argument('action', choices=('status', 'start', 'stop'), nargs='?', default='status')
    play.add_argument('--force', action='store_true', help='stop a Play session someone else started')
    studio_option(play)

    out = command('output', 'console errors and warnings (use --all for every line)')
    out.add_argument('--all', action='store_true')
    studio_option(out)

    view = command('view', 'screenshot the game viewport, or render an instance from several angles')
    view.add_argument('path', nargs='?', default='game')
    view.add_argument('--output', default=None)
    view.add_argument('--rig', default=None)
    view.add_argument('--angle', default=None)
    view.add_argument('--samples', type=int, default=9)
    view.add_argument('--appearance', choices=('avatar', 'noob'), default='avatar')
    view.add_argument('--rigType', dest='rig_type', choices=('R6', 'R15'), default=None)
    view.add_argument('--cameraCframe', dest='camera_cframe', type=json.loads, default=None)
    view.add_argument('--contactSheet', dest='contact_sheet', action='store_true', help='one view per child model')
    studio_option(view)

    capture = command('capture', 'short timestamped screenshot sequence, optionally sampling game state each frame')
    capture.add_argument('--frames', type=int, default=6)
    capture.add_argument('--interval', type=int, default=500, help='milliseconds between frames')
    capture.add_argument('--state', default=None, help='Luau expression evaluated after every frame')
    capture.add_argument('--server', dest='context', action='store_const', const='server', default='client', help='evaluate --state on server')
    capture.add_argument('--output', default=None, help='folder for the frames')
    studio_option(capture)

    profile = command('profile', 'per-function call counts and timing for modules during Play')
    profile.add_argument('modules', nargs='+', help='dotted paths of modules or folders')
    profile.add_argument('--seconds', type=float, default=5)
    profile.add_argument('--only', default=None, help='comma-separated function names to time')
    profile.add_argument('--server', dest='context', action='store_const', const='server', default='client')
    studio_option(profile)

    # Inspect and edit
    listing = command('ls', 'list the Studio instance tree')
    listing.add_argument('path', nargs='?', default='game')
    listing.add_argument('--recursive', action='store_true')
    listing.add_argument('--limit', type=int, default=200)
    listing.add_argument('--cursor', default=None)
    studio_option(listing)

    detail = command('cat', 'show one instance')
    detail.add_argument('path')
    studio_option(detail)

    find = command('find', 'find instances by name or class')
    find.add_argument('path', nargs='?', default='game')
    find.add_argument('--name', default=None)
    find.add_argument('--class', dest='className', default=None)
    find.add_argument('--limit', type=int, default=200)
    find.add_argument('--cursor', default=None)
    studio_option(find)

    run_server = command('runServer', 'run Luau headlessly on the server in Run mode (no window); Edit mode only')
    run_server.add_argument('code', help='function body; its return value is reported')
    run_server.add_argument('--timeout', type=float, default=30)
    studio_option(run_server)

    grep = command('grep', 'search script source in Studio')
    grep.add_argument('query')
    grep.add_argument('--pattern', dest='plain', action='store_false', default=True, help='treat query as a Luau pattern (default: plain text)')
    grep.add_argument('--caseSensitive', dest='ignore_case', action='store_false', default=True, help='case-sensitive match (default: case-insensitive)')
    grep.add_argument('--ignoreCase', dest='ignore_case', action='store_true', default=True, help=argparse.SUPPRESS)
    grep.add_argument('--limit', type=int, default=200)
    grep.add_argument('--skip', type=int, default=0)
    studio_option(grep)

    coverage = command('coverage', 'census of instances and properties in Studio')
    coverage.add_argument('--root', default='game')
    studio_option(coverage)

    camera = command('camera', 'show or reset the Edit camera')
    camera.add_argument('action', choices=('status', 'reset'), nargs='?', default='status')
    studio_option(camera)

    from .tools import configure_cli
    configure_cli(sub)

    # Assets, publishing, plugin
    command('libraryOptions', 'Creator Store search filters')
    library = command('librarySearch', 'search the Creator Store')
    library.add_argument('query', nargs='?', default='')
    library.add_argument('--category', default='Model')
    library.add_argument('--filters', type=json.loads, default=None)
    library.add_argument('--output', default='.screenshots/library.png')

    addition = command('libraryAdd', 'insert a Creator Store asset into a quarantine folder')
    addition.add_argument('assetId')
    addition.add_argument('--name', default=None)
    studio_option(addition)

    upload = command('upload', 'upload a file (or a game.* instance) to Roblox Open Cloud')
    upload.add_argument('file')
    upload.add_argument('--creatorId', dest='creator_id', default=None, help='creator ID (default: active studio user ID or auth config)')
    upload.add_argument('--creatorType', dest='creator_type', choices=('user', 'group'), default='user')
    upload.add_argument('--assetType', dest='asset_type', default=None, help='asset type (default: inferred from file extension)')
    upload.add_argument('--name', default=None, help='display name (default: filename stem or instance name)')
    upload.add_argument('--description', default='')
    upload.add_argument('--waitSeconds', dest='wait_seconds', type=float, default=60)
    studio_option(upload)

    command('uploadStatus', 'check an upload operation').add_argument('operation')

    for name in ('createGamePass', 'createDeveloperProduct'):
        item = command(name, f'create a {name[6:]}')
        item.add_argument('name', nargs='?', default=None, help='item name')
        item.add_argument('--name', dest='named_name', default=None, help='item name override')
        item.add_argument('--universeId', dest='universe_id', default=None, help='universe ID (default: auth config or env)')
        item.add_argument('--description', default='')
        item.add_argument('--icon', default=None)
        item.add_argument('--price', type=int, default=None, help='price in Robux (automatically sets forSale=True)')
        item.add_argument('--forSale', dest='for_sale', action='store_true', default=None)
        item.add_argument('--notForSale', dest='not_for_sale', action='store_true', default=False)
        item.add_argument('--regionalPricing', dest='regional_pricing', action='store_true', default=False)

    export = command('exportAsset', 'export an instance as .rbxm')
    export.add_argument('path')
    export.add_argument('--output', default=None, help='output .rbxm file path (default: <name>.rbxm in current directory)')
    studio_option(export)

    command('publishModel', 'publish a model asset').add_argument('asset_id')
    save = command('savePlugin', 'rebuild and install the Studio receiver plugin')
    save.add_argument('directory', nargs='?', default=None)
    return parser


def server_up(port):
    try:
        with urlopen(f'http://127.0.0.1:{port}/health', timeout=2) as response:
            return json.load(response).get('service') == 'StudioMCP'
    except (URLError, OSError, ValueError):
        return False


def start_server(port):
    flags = getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    root = str(Path(os.path.abspath(__file__)).parents[1])
    env = {**os.environ, 'PYTHONPATH': root + (os.pathsep + os.environ['PYTHONPATH'] if os.environ.get('PYTHONPATH') else '')}
    subprocess.Popen([sys.executable, '-m', 'studiomcp', '--port', str(port), 'serve', '--mcp', 'http', '--mcpPort', str(port + 1)],
                     cwd=str(Path.home()), env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if server_up(port):
            return True
        time.sleep(0.4)
    return False


def call(port, operation, arguments):
    request = Request(f'http://127.0.0.1:{port}/operation', json.dumps({'operation': operation, 'arguments': arguments}).encode(),
                      {'Content-Type': 'application/json'})
    for attempt in range(4):
        if attempt == 0 and not server_up(port):
            print('studiomcp server was not running; starting it...', file=sys.stderr)
            if not start_server(port):
                raise SystemExit(fail('Could not start the StudioMCP server (is the port used by something else?)', EXIT_UNAVAILABLE))
        try:
            with urlopen(request, timeout=None) as response:
                return json.load(response)
        except HTTPError as error:
            body = error.read().decode(errors='replace')
            try:
                detail = json.loads(body)
            except ValueError:
                detail = {'error': body}
            raise SystemExit(fail(detail.get('error', body), exit_code(detail.get('code', 'failed'))))
        except (URLError, ConnectionError, OSError):
            time.sleep(2)
    raise SystemExit(fail('The StudioMCP server stopped answering.', EXIT_UNAVAILABLE))


def fail(message, code):
    print(message, file=sys.stderr)
    return code


def unwrap(reply):
    if isinstance(reply, dict) and reply.get('state') == 'done' and 'result' in reply and 'id' in reply:
        return reply['result']
    return reply


def prepare(args):
    arguments = {key: value for key, value in vars(args).items() if key not in ('command', 'port', 'json', 'func')}
    operation = {'play': 'playtest', 'studios': 'status'}.get(args.command, args.command)
    arguments['owner'] = 'cli'
    if operation == 'view':
        arguments['output'] = str(Path(arguments['output'] or '.screenshots/view.png').resolve())
    if operation == 'librarySearch' and arguments.get('output'):
        arguments['output'] = str(Path(arguments['output']).resolve())
    if operation == 'capture' and arguments.get('output'):
        arguments['output'] = str(Path(arguments['output']).resolve())
    if operation == 'profile' and arguments.get('only'):
        arguments['only'] = [name.strip() for name in arguments['only'].split(',') if name.strip()]
    if operation == 'operation':
        arguments['wait'] = args.wait
    if operation == 'output':
        arguments.pop('all', None)
    if operation == 'find':
        target = arguments.get('path', 'game')
        if target != 'game' and not arguments.get('name') and not (target.startswith('game') or '.' in target or '/' in target):
            arguments['name'] = target
            arguments['path'] = 'game'
    if operation in ('createGamePass', 'createDeveloperProduct'):
        item_name = arguments.pop('named_name', None) or arguments.get('name')
        if not item_name:
            raise ValueError(f'{operation} requires a name')
        arguments['name'] = item_name
        if arguments.get('not_for_sale'):
            arguments['for_sale'] = False
        elif arguments.get('for_sale') is None:
            arguments['for_sale'] = bool(arguments.get('price') is not None)
        arguments.pop('not_for_sale', None)
    return operation, arguments


def main():
    parser = build_parser()
    args = parser.parse_args()
    if args.command == 'serve':
        return serve(parser, args)
    operation, arguments = prepare(args)
    reply = call(args.port, operation, arguments)
    if isinstance(reply, dict) and reply.get('state') == 'running' and 'id' in reply and operation != 'operation':
        print(f"Still running after 30 s as {reply['id']} ({reply['operation']}). Fetch it with:\n  studiomcp operation {reply['id']} --wait")
        raise SystemExit(EXIT_TIMEOUT)
    shown = operation
    if operation == 'operation':
        if reply.get('state') == 'running':
            print(f"{reply['id']} ({reply['operation']}) is still running after {reply['elapsedSeconds']}s; rerun to keep waiting.")
            raise SystemExit(EXIT_TIMEOUT)
        if reply.get('state') == 'failed':
            raise SystemExit(fail(f"{reply['id']} ({reply['operation']}) failed: {reply['error']}", EXIT_FAILED))
        shown, reply = reply.get('operation', operation), reply.get('result', reply)
    if args.json:
        print(json.dumps(reply, indent=2))
        raise SystemExit(EXIT_FAILED if report.failed(shown, reply) else EXIT_OK)
    text = report.render(shown, reply, {**vars(args), **arguments})
    print(text)
    raise SystemExit(EXIT_FAILED if report.failed(shown, reply) else EXIT_OK)


def serve(parser, args):
    import threading
    from .environment import Environment
    try:
        environment = Environment(args.port)
    except OSError as error:
        parser.exit(EXIT_FAILED, f'Cannot start StudioMCP on 127.0.0.1:{args.port}: {error}. Another server may own this port; use it or stop it first.\n')
    try:
        if not args.mcp:
            print(f'StudioMCP listening on 127.0.0.1:{args.port}', flush=True)
            threading.Event().wait()
        else:
            try:
                from .mcp import create_server
            except ImportError:
                if args.mcp == 'stdio':
                    raise
                print(f'StudioMCP listening on 127.0.0.1:{args.port}', flush=True)
                threading.Event().wait()
            else:
                create_server(environment, args.mcp_port).run(transport='stdio' if args.mcp == 'stdio' else 'streamable-http')
    except KeyboardInterrupt:
        pass
    finally:
        environment.close()
    return EXIT_OK
