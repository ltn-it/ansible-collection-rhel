"""Component-wide log archive store.

One store belongs to one component. Callers serialize native archive claims and
operational-log rollover using this store's lock. Native archive slots are
reserved explicitly. Two further slots are reserved for claimed raw input
and compression output, including interrupted work. This deliberately retains
fewer than seven published archives to keep the seven-file ceiling during work.
"""

import contextlib
import fcntl
import gzip
import hashlib
import os
from pathlib import Path
import re
import stat
import time


ARCHIVE = re.compile(r"a\.[0-9]+\.[0-9a-f]{32}\.gz\Z")
RAW = "incoming.raw"
TEMP = "incoming.gz.tmp"


def regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("unsafe log file")
    return info


def closed_inode(info, writer_pids):
    """Fail closed if any named writer still holds this inode or cannot be read."""
    identity = (info.st_dev, info.st_ino)
    for pid in writer_pids:
        handles = Path("/proc") / str(pid) / "fd"
        try:
            opened_handles = list(handles.iterdir())
        except FileNotFoundError:
            continue  # exited processes cannot retain an open file description
        except PermissionError:
            try:
                state = (handles.parent / "stat").read_text().rsplit(")", 1)[1].split()[0]
            except FileNotFoundError:
                continue
            if state == "Z":
                continue  # exit has released every descriptor; waitpid is pending
            raise
        for handle in opened_handles:
            try:
                opened = handle.stat()
            except FileNotFoundError:
                continue
            if (opened.st_dev, opened.st_ino) == identity:
                return False
    return True


class ArchiveStore:
    def __init__(self, directory, native_archives, active_files,
                 max_file=100 * 1024**2, max_total=1024**3,
                 max_archives=7, max_age=14 * 86400):
        self.root = Path(directory)
        info = self.root.lstat()
        if (self.root.resolve() != self.root or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError("archive directory must be owned, private and canonical")
        if not (1 <= active_files <= 2 and 0 <= native_archives <= 2):
            raise ValueError("unsupported source budget")
        if (type(max_file) is not int or not 1 <= max_file <= 100 * 1024**2
                or type(max_archives) is not int or not 1 <= max_archives <= 7
                or type(max_total) is not int or not 1 <= max_total <= 1024**3
                or not 0 < max_age <= 14 * 86400):
            raise ValueError("policy cannot exceed approved ceilings")
        self.published_limit = max_archives - native_archives - 2
        # Conservative gzip overhead allowance, including incompressible input.
        self.gzip_limit = max_file + max_file // 1000 + 1024
        worst = ((active_files + native_archives + 1) * max_file
                 + (self.published_limit + 1) * self.gzip_limit)
        if self.published_limit < 1 or worst > max_total or max_age <= 0:
            raise ValueError("unsafe storage budget")
        self.max_file = max_file
        self.max_age = max_age

    @contextlib.contextmanager
    def locked(self):
        fd = os.open(self.root / "store.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("unsafe store lock")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield self
        finally:
            os.close(fd)

    def sync(self, directory=None):
        fd = os.open(self.root if directory is None else directory,
                     os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def archives(self):
        result = []
        for item in self.root.iterdir():
            if item.name in (RAW, TEMP, "store.lock"):
                regular(item)
            elif ARCHIVE.fullmatch(item.name):
                result.append((regular(item).st_mtime_ns, item))
            else:
                raise ValueError("unexpected file in managed archive store")
        return sorted(result)

    def prune(self, now=None):
        now = time.time() if now is None else now
        items = self.archives()
        retained = []
        for stamp, item in items:
            if stamp / 1e9 <= now - self.max_age:
                item.unlink()
            else:
                retained.append(item)
        for item in retained[:max(0, len(retained) - self.published_limit)]:
            item.unlink()
        self.sync()

    def claim(self, source):
        source = Path(source)
        self.prune()
        raw = self.root / RAW
        if raw.exists():
            raise FileExistsError("recover claimed input before accepting another")
        info = regular(source)
        if (source.resolve() != source or info.st_uid != os.geteuid()
                or info.st_dev != self.root.stat().st_dev or info.st_size > self.max_file):
            raise ValueError("unsupported source filesystem or size")
        os.rename(source, raw)
        self.sync()
        self.sync(source.parent)

    def compress(self, writer_pids):
        """Compress only closed, immutable input; recover .tmp by regenerating it."""
        self.prune()
        raw, temporary = self.root / RAW, self.root / TEMP
        if temporary.exists():
            regular(temporary)
            temporary.unlink()
        if not raw.exists():
            return None
        info = regular(raw)
        if info.st_size > self.max_file:
            raise ValueError("claimed input exceeds file ceiling")
        if not closed_inode(info, writer_pids):
            return None
        fd = os.open(raw, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            fcntl.lockf(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            before = os.fstat(fd)
            if (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError("claimed input changed identity")
            digest = hashlib.sha256()
            count = 0
            out_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(out_fd, "wb") as output:
                with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
                    while True:
                        block = os.read(fd, 65536)
                        if not block:
                            break
                        count += len(block)
                        if count > self.max_file:
                            raise ValueError("input grew during compression")
                        digest.update(block)
                        compressed.write(block)
                output.flush()
                os.fsync(output.fileno())
            after = os.fstat(fd)
            if ((before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise ValueError("input mutated during compression")
            if count != before.st_size or temporary.stat().st_size > self.gzip_limit:
                raise ValueError("compression budget exceeded")
            verify = hashlib.sha256()
            restored = 0
            with gzip.open(temporary, "rb") as decoded:
                while True:
                    block = decoded.read(65536)
                    if not block:
                        break
                    restored += len(block)
                    if restored > count:
                        raise ValueError("decompression size mismatch")
                    verify.update(block)
            if restored != count or verify.digest() != digest.digest():
                raise ValueError("decompression checksum mismatch")
            # A crash after publication but before unlinking raw must not consume
            # retention slots with duplicate copies during recovery.
            identity = ("%d:%d:%d:%d:%d:" % (
                before.st_dev, before.st_ino, before.st_mtime_ns,
                before.st_ctime_ns, before.st_size)).encode()
            marker = hashlib.sha256(identity + digest.digest()).hexdigest()[:32]
            destination = self.root / ("a.%d.%s.gz" % (before.st_mtime_ns, marker))
            if destination.exists():
                existing = regular(destination)
                if existing.st_size != temporary.stat().st_size:
                    raise ValueError("published recovery archive differs")
                with destination.open("rb") as left, temporary.open("rb") as right:
                    while True:
                        expected = right.read(65536)
                        if left.read(65536) != expected:
                            raise ValueError("published recovery archive differs")
                        if not expected:
                            break
                temporary.unlink()
                raw.unlink()
                self.sync()
                self.prune()
                return destination
            # Make room before publishing: raw + temporary and native slots are
            # already reserved, so no successful step depends on later pruning.
            items = self.archives()
            for _, item in items[:max(0, len(items) - self.published_limit + 1)]:
                item.unlink()
            os.utime(temporary, ns=(before.st_mtime_ns, before.st_mtime_ns))
            os.rename(temporary, destination)
            self.sync()
            raw.unlink()
            self.sync()
            self.prune()
            return destination
        finally:
            os.close(fd)
