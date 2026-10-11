"""Automatic ownership of pending Studio work and agent-started Play sessions."""
import threading
import time
from collections import deque

from .errors import StudioBusy

READ_ONLY = {'ls', 'cat', 'find', 'grep', 'output', 'readScript', 'searchScripts', 'coverage', 'playtest-status'}


class Events:
    def __init__(self, size=300):
        self.items = deque(maxlen=size)
        self.lock = threading.Lock()

    def record(self, kind, message, **details):
        with self.lock:
            self.items.append({'time': round(time.time(), 1), 'kind': kind, 'message': message, **details})

    def recent(self, limit=50, since=None):
        with self.lock:
            items = list(self.items)
        if since is not None:
            items = [item for item in items if item['time'] >= since]
        return items[-limit:]


class Lease:
    def __init__(self, events, clock=time.time):
        self.events = events
        self.clock = clock
        self.pending = {}
        self.play_owner = None
        self.guard = threading.RLock()

    def status(self):
        with self.guard:
            work = next(iter(self.pending.values()), None)
            owner = work['owner'] if work else self.play_owner
            view = {'locked': owner is not None, 'playStartedBy': self.play_owner}
            if owner is not None:
                view.update(owner=owner, reason=work['operation'] if work else 'Play',
                            pending=len(self.pending))
            return view

    def check(self, operation, owner):
        if operation in READ_ONLY:
            return
        with self.guard:
            if self.pending:
                work = next(iter(self.pending.values()))
                raise StudioBusy(f"Studio has pending/running {work['operation']} by {work['owner']}; fetch its operation receipt and retry when it finishes.")
            if self.play_owner and self.play_owner != owner:
                raise StudioBusy(f'Studio Play is owned by {self.play_owner}; wait for that session to end.')

    def begin(self, operation, owner, force=False):
        if operation in READ_ONLY:
            return None
        with self.guard:
            if operation == 'playtest-stop' and force:
                if self.pending:
                    raise StudioBusy('Studio has pending/running work; wait for it to finish before stopping Play.')
            else:
                self.check(operation, owner)
            token = object()
            self.pending[token] = {'owner': owner, 'operation': operation}
            return token

    def finish(self, token):
        with self.guard:
            self.pending.pop(token, None)

    def reconcile(self, targets):
        with self.guard:
            if not self.pending and targets and all(target['mode'] == 'Edit' for target in targets):
                self.play_owner = None

    def note_play(self, started, owner):
        with self.guard:
            self.play_owner = owner if started else None

    def check_stop(self, owner, force=False):
        with self.guard:
            if self.play_owner and owner != self.play_owner and not force:
                raise StudioBusy(f'The Play session was started by {self.play_owner}; only they should stop it (pass --force to override).')
