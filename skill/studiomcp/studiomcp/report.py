"""Turn server replies into short human text. `--json` bypasses all of this.

Each renderer answers the question the caller actually has ("did it work, what changed, what
do I do next?") instead of echoing internal fields.
"""
import json


def plural(count, word):
    return f'{count} {word}' if count == 1 else f'{count} {word}s'


def failed(operation, result):
    """Whether the operation finished but did not succeed (drives the exit code)."""
    if not isinstance(result, dict):
        return False
    if operation in ('execute', 'runServer'):
        return result.get('success') is False
    return False


def render(operation, result, options=None):
    options = options or {}
    renderer = RENDERERS.get(operation)
    if renderer is None:
        return json.dumps(result, indent=2)
    return renderer(result, options)


def status(result, options):
    lines = []
    if not result['studios']:
        lines.append('No Studio is connected. Open Studio with the StudioMCP plugin installed; commands wait up to 20 s for it.')
    for target in result['studios']:
        contexts = ', '.join(target['contexts'])
        lines.append(f"Studio {target['index']}: {target.get('name') or 'unnamed'}  [{target['mode']}]  contexts: {contexts}")
    lock = result['lock']
    lines.append(f"Lock: {lock['owner']} ({lock['reason']})" if lock['locked'] else 'Lock: free')
    if lock.get('playStartedBy'):
        lines.append(f"Play session started by {lock['playStartedBy']}")
    for operation in result['running']:
        lines.append(f"Running: {operation['id']} {operation['operation']} ({operation['elapsedSeconds']}s)")
    if result['recentEvents']:
        lines.append('Recent:')
        lines += [f"  {event['kind']:8} {event['message']}" for event in result['recentEvents'][-5:]]
    return '\n'.join(lines)


def events(result, options):
    import time
    return '\n'.join(f"{time.strftime('%H:%M:%S', time.localtime(e['time']))} {e['kind']:8} {e['message']}" for e in result) or 'No events yet.'


def operations(result, options):
    return '\n'.join(f"{o['id']}  {o['operation']:10} {o['state']:8} {o['elapsedSeconds']}s" for o in result) or 'No operations yet.'


def lock(result, options):
    if not result['locked']:
        return 'Studio lock is free.'
    return f"Studio is locked by {result['owner']} for {result['heldSeconds']}s; it expires in {result['expiresInSeconds']}s."


def console(summary):
    """The condensed console block shared by playtest and output."""
    if 'unavailable' in summary:
        return f"Console: unavailable ({summary['unavailable']})"
    if not summary['errors'] and not summary['warnings']:
        return 'Console: clean (no errors or warnings)'
    lines = [f"Console: {plural(summary['errors'], 'error')}, {plural(summary['warnings'], 'warning')}"]
    lines += [f"  {item['type']:7} x{item['count']}  {item['message']}" for item in summary['distinct']]
    return '\n'.join(lines)


def playtest(result, options):
    if 'readySeconds' in result:
        head = f"Play started in {result['readySeconds']} s (viewport {int(result['viewport'][0])}x{int(result['viewport'][1])})."
    else:
        head = f"Studio is in {result.get('mode', '?')}."
    return head + ('\n' + console(result['output']) if 'output' in result else '')


def output(result, options):
    entries = result.get('entries', [])
    if options.get('all'):
        return '\n'.join(f"{str(e.get('type', '')).rsplit('.', 1)[-1]:16} {e.get('message', '')}" for e in entries) or 'Output is empty.'
    counts = {}
    for entry in entries:
        kind = str(entry.get('type', '')).rsplit('.', 1)[-1].replace('Message', '')
        if kind in ('Error', 'Warning'):
            key = (kind, entry.get('message', '')[:300])
            counts[key] = counts.get(key, 0) + 1
    summary = {'errors': sum(n for (k, _), n in counts.items() if k == 'Error'), 'warnings': sum(n for (k, _), n in counts.items() if k == 'Warning'),
               'distinct': [{'type': k, 'count': n, 'message': m} for (k, m), n in sorted(counts.items(), key=lambda i: (i[0][0] != 'Error', -i[1]))[:15]]}
    return console(summary) + f"\n({len(entries)} lines in total; --all shows them)"


def execute(result, options):
    lines = []
    for text in result.get('prints', []):
        lines.append(str(text))
    if result.get('success') is False:
        lines.append('Error: ' + str(result.get('message')))
    for value in result.get('results', []):
        lines.append(json.dumps(value, indent=2) if isinstance(value, (dict, list)) else str(value))
    if result.get('sideEffects'):
        lines.append(f"Note: this run changed {', '.join(result['sideEffects'])}; StudioMCP restored it. {result.get('note', '')}")
    return '\n'.join(lines) or 'ok (no output)'


def view(result, options):
    return result.get('path') or 'Captured (no output path given).'


def capture(result, options):
    lines = [f"{len(result['frames'])} frames in {result['directory']}"]
    for frame in result['frames']:
        lines.append(f"  {frame['ms']:>6} ms  {frame['file'].rsplit(chr(92), 1)[-1]}" + (f"  state={json.dumps(frame['state'])}" if frame['state'] is not None else ''))
    if result.get('sheet'):
        lines.append('Contact sheet: ' + result['sheet'])
    return '\n'.join(lines)


def profile(result, options):
    return result.get('text', '')


RENDERERS = {'status': status, 'events': events, 'operations': operations, 'lock': lock,
             'playtest': playtest, 'output': output, 'execute': execute, 'view': view, 'capture': capture, 'profile': profile}
