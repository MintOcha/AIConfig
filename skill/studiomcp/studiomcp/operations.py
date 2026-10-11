"""Long Studio operations that survive a dropped caller.

Every server operation runs on a worker thread. The caller waits up to `QUICK_SECONDS`
for the answer; if the work is still running it gets an operation ID instead, and the
finished receipt (result or error) is kept in memory and on disk so it can be fetched
later with `romcp operation <id>` without repeating the work.
"""
import json
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

QUICK_SECONDS = 30
KEEP_RECEIPTS = 100


def receipts_directory():
    path = Path.home() / '.studiomcp' / 'operations'
    path.mkdir(parents=True, exist_ok=True)
    return path


class Operations:
    def __init__(self, directory=None, quick_seconds=QUICK_SECONDS):
        self.directory = Path(directory) if directory else receipts_directory()
        self.quick_seconds = quick_seconds
        self.lock = threading.Lock()
        self.records = OrderedDict()

    def run(self, name, function, wait=None):
        """Run `function()`; return its result if quick, else a running-operation receipt."""
        record = {'id': 'op-' + uuid.uuid4().hex[:10], 'operation': name, 'state': 'running',
                  'started': time.time(), 'done': threading.Event()}
        with self.lock:
            self.records[record['id']] = record
            while len(self.records) > KEEP_RECEIPTS:
                self.records.popitem(last=False)
        threading.Thread(target=self._work, args=(record, function), daemon=True).start()
        record['done'].wait(self.quick_seconds if wait is None else wait)
        if record['state'] == 'running':
            return self.receipt(record)
        return self._outcome(record)

    def _work(self, record, function):
        try:
            record['result'] = function()
            record['state'] = 'done'
        except BaseException as error:  # a failed operation is a result, never a crashed thread
            record['error'] = f'{type(error).__name__}: {error}'
            record['state'] = 'failed'
        record['finished'] = time.time()
        self._save(record)
        record['done'].set()

    def _outcome(self, record):
        if record['state'] == 'failed':
            raise RuntimeError(record['error'])
        return record['result']

    def receipt(self, record):
        """Public, JSON-safe view of an operation."""
        view = {'id': record['id'], 'operation': record['operation'], 'state': record['state'],
                'elapsedSeconds': round((record.get('finished') or time.time()) - record['started'], 1)}
        if record['state'] == 'running':
            view['hint'] = f"Still running. Fetch the result with: romcp operation {record['id']} --wait"
        elif record['state'] == 'done':
            view['result'] = record['result']
        else:
            view['error'] = record['error']
        return view

    def get(self, operation_id, wait=0):
        """Receipt for an operation, optionally waiting up to `wait` seconds for it to finish."""
        with self.lock:
            record = self.records.get(operation_id)
        if record is None:
            saved = self.directory / (operation_id + '.json')
            if saved.is_file():
                return json.loads(saved.read_text(encoding='utf-8'))
            raise ValueError(f'Unknown operation {operation_id}')
        if wait and record['state'] == 'running':
            record['done'].wait(wait)
        return self.receipt(record)

    def listing(self):
        with self.lock:
            records = list(self.records.values())
        return [{key: value for key, value in self.receipt(record).items() if key != 'result'} for record in records]

    def _save(self, record):
        """Persist the final receipt; results may be large so oversized ones are summarised."""
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            view = self.receipt(record)
            text = json.dumps(view)
            if len(text) > 2_000_000:
                view['result'] = {'truncated': True, 'bytes': len(text)}
                text = json.dumps(view)
            (self.directory / (record['id'] + '.json')).write_text(text, encoding='utf-8')
            old = sorted(self.directory.glob('op-*.json'), key=lambda path: path.stat().st_mtime)[:-KEEP_RECEIPTS]
            for path in old:
                path.unlink(missing_ok=True)
        except OSError:
            pass  # receipts are a convenience; never fail the operation over disk trouble
