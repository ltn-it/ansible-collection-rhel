"""Size- and calendar-bounded capture of operational command output.

ArchiveStore's native PID provider must cover every native writer of that
component, including while recovering pending input. Callers must not bypass the
component lock or write directly to the operational log. No shell is invoked.
"""

import datetime
import contextlib
import os
from pathlib import Path
import selectors
import subprocess
import time

from archive_store import RAW, regular


class PendingWriter(Exception):
    pass


class BoundedFile:
    def __init__(self, store, active, writer_pids, clock=time.time):
        self.store = store
        self.active = Path(active)
        if self.active.resolve() != self.active or self.active.parent.stat().st_dev != store.root.stat().st_dev:
            raise ValueError("active log requires canonical path on archive filesystem")
        parent = self.active.parent.stat()
        if parent.st_uid != os.geteuid() or parent.st_mode & 0o777 != 0o700:
            raise ValueError("active log parent must be owned and private")
        self.writer_pids = writer_pids
        self.clock = clock
        # A fresh capture invocation rotates existing nonempty output. This also
        # gives safe restart behavior without guessing a lost creation date.
        self.day = None

    def today(self):
        return datetime.datetime.fromtimestamp(self.clock()).astimezone().date()

    def recover(self):
        self.store.compress(self.writer_pids())
        if (self.store.root / RAW).exists():
            raise PendingWriter("native writer has not yet closed claimed input")

    def rollover(self):
        self.recover()
        if self.active.exists():
            info = regular(self.active)
            if info.st_uid != os.geteuid() or info.st_size > self.store.max_file:
                raise ValueError("unsafe active operational log")
            if info.st_size:
                self.store.claim(self.active)
                self.recover()
        self.day = self.today()

    def tick(self):
        """Call under component lock, including while a child emits no output."""
        self.store.prune(self.clock())
        if self.day != self.today():
            self.rollover()

    def write(self, data):
        """Call under component lock. Split binary chunks at the hard ceiling."""
        self.tick()
        while data:
            if self.active.exists():
                info = regular(self.active)
                if info.st_uid != os.geteuid() or info.st_size > self.store.max_file:
                    raise ValueError("unsafe active operational log")
                current = info.st_size
            else:
                current = 0
            if current == self.store.max_file:
                self.rollover()
                current = 0
            amount = min(len(data), self.store.max_file - current)
            fd = os.open(self.active, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                info = os.fstat(fd)
                if info.st_uid != os.geteuid() or info.st_nlink != 1 or (info.st_mode & 0o777) != 0o600:
                    raise ValueError("unsafe opened operational log")
                chunk = memoryview(data)[:amount]
                while chunk:
                    written = os.write(fd, chunk)
                    if written <= 0:
                        raise OSError("operational log write made no progress")
                    chunk = chunk[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            data = data[amount:]


def capture(command, bounded, retry_seconds=10):
    """Preserve child status. Logging failure stops this child, never other jobs."""
    def guarded(action, retry_pending=True):
        deadline = time.monotonic() + retry_seconds
        while True:
            try:
                with contextlib.ExitStack() as stack:
                    try:
                        stack.enter_context(bounded.store.locked())
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("component log lock unavailable")
                    else:
                        # Only acquisition may be retried on BlockingIOError.
                        # Exceptions after action begins must retain partial-write
                        # semantics instead of duplicating a previously written prefix.
                        return action()
            except PendingWriter:
                if not retry_pending or time.monotonic() >= deadline:
                    raise
            time.sleep(0.05)

    # Refuse invalid/unrecoverable logging before starting a backup or restore.
    guarded(bounded.tick)
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        with selectors.DefaultSelector() as events:
            events.register(child.stdout, selectors.EVENT_READ)
            while events.get_map():
                ready = events.select(timeout=1)
                if not ready:
                    guarded(bounded.tick)
                    continue
                block = os.read(child.stdout.fileno(), 65536)
                if block:
                    # A data write may already have appended a prefix. Never
                    # replay its whole chunk after an interrupted rollover.
                    guarded(lambda: bounded.write(block), retry_pending=False)
                else:
                    events.unregister(child.stdout)
        return child.wait()
    except BaseException:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)
        raise
    finally:
        child.stdout.close()
