"""Job state shared by the sync worker and the API, and the cross-process gate."""

import threading
from datetime import datetime
from uuid import uuid4

from local_files import try_lock
from planner_data import SyncStopped


def now():
    """Current local time as an ISO string, to the second."""
    return datetime.now().astimezone().isoformat(timespec='seconds')


class Job:
    """One running or finished sync, preview or download."""

    def __init__(self, mode, scope, candidates=()):
        """Create a job with its scope and any preselected documents."""
        self.id = uuid4().hex
        self.mode = mode
        self.scope = scope
        self.started = now()
        self.stop = threading.Event()
        self.statistics = {}
        self.candidates = {}
        self.selected = list(candidates)
        self.rows = {}
        self.matches = []
        self.modules = []
        self.errors = []

    def check(self):
        """Raise SyncStopped if the user asked to stop."""
        if self.stop.is_set():
            raise SyncStopped()

    def public(self):
        """The job details that are safe to show in the API."""
        return dict(
            id=self.id,
            mode=self.mode,
            started=self.started,
            stopping=self.stop.is_set(),
            metrics=self.statistics.copy(),
        )


class ProcessGate:
    """An OS lock shared by all local server processes and released on exit."""

    def __init__(self):
        """Start without holding the lock."""
        self.stream = None

    def acquire(self, root):
        """Take the operation lock in the data folder; False if another process has it."""
        self.stream = try_lock(root / '.planner-operation.lock')
        return self.stream is not None

    def release(self):
        """Release the operation lock."""
        if self.stream:
            self.stream.close()
            self.stream = None
