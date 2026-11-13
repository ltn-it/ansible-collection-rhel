"""Bounded archive-inode metadata reader and required runtime client.

Caller supplies trusted inode metadata and runtime UID. A False result means a
matching descriptor was observed. True is only a bounded scan observation, not
an atomic guarantee against descriptor transfer or newly opened files. The caller
must retain its component lock and immutable-input checks during compression.
"""
import contextlib
import os
import time
import json
import socket
import stat
import struct
from pathlib import Path


class ScanRefused(RuntimeError):
    """Metadata visibility or boundedness could not be established."""


@contextlib.contextmanager
def directory(name, parent=None):
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                 dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


def runtime_uids(process):
    """Read only bounded credential metadata, never command line or FD data."""
    fd = os.open('status', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=process)
    try:
        raw = os.read(fd, 65537)
    finally:
        os.close(fd)
    if len(raw) > 65536:
        raise ScanRefused('status_bound')
    rows = [row.split()[1:] for row in raw.splitlines()
            if row.startswith(b'Uid:')]
    if (len(rows) != 1 or len(rows[0]) != 4
            or not all(value.isdigit() for value in rows[0])):
        raise ScanRefused('uid_schema')
    return tuple(map(int, rows[0]))


def scan_inode(uid, device, inode, *, proc='/proc', seconds=5,
               max_processes=8192, max_tasks=4096, max_descriptors=65536,
               readability_only=False):
    """Scan all threads of processes with any matching real/effective/save/fs UID.

    Anchored directory FDs prevent a recycled numeric PID from redirecting an
    in-progress scan. Missing processes/tasks/handles are exit/close races;
    unreadable metadata, changed credentials and exhausted bounds refuse.
    """
    if (type(uid) is not int or uid <= 0 or type(device) is not int
            or device < 0 or type(inode) is not int or inode <= 0
            or type(readability_only) is not bool):
        raise ScanRefused('input_schema')
    deadline = time.monotonic() + seconds
    counts = dict(processes=0, selected=0, tasks=0, descriptors=0)

    def bound(key, limit):
        counts[key] += 1
        if counts[key] > limit or time.monotonic() >= deadline:
            raise ScanRefused('scan_bound')

    try:
        with directory(proc) as root:
            # scandir is deliberately streamed; no unbounded /proc list.
            with os.scandir(root) as processes:
                for entry in processes:
                    if not entry.name.isdecimal():
                        continue
                    bound('processes', max_processes)
                    try:
                        with directory(entry.name, root) as process:
                            credentials = runtime_uids(process)
                            if uid not in credentials:
                                continue
                            counts['selected'] += 1
                            with directory('task', process) as task_root:
                                with os.scandir(task_root) as tasks:
                                    for task in tasks:
                                        if not task.name.isdecimal():
                                            raise ScanRefused('task_schema')
                                        bound('tasks', max_tasks)
                                        try:
                                            with directory(task.name, task_root) as thread:
                                                if uid not in runtime_uids(thread):
                                                    raise ScanRefused('credentials_changed')
                                                with directory('fd', thread) as handles:
                                                    with os.scandir(handles) as entries:
                                                        for handle in entries:
                                                            bound('descriptors', max_descriptors)
                                                            if not handle.name.isdecimal():
                                                                raise ScanRefused('fd_schema')
                                                            try:
                                                                info = os.stat(handle.name, dir_fd=handles)
                                                            except FileNotFoundError:
                                                                continue
                                                            if (not readability_only and
                                                                    (info.st_dev, info.st_ino) == (device, inode)):
                                                                return False, counts
                                                if uid not in runtime_uids(thread):
                                                    raise ScanRefused('credentials_changed')
                                        except (FileNotFoundError, ProcessLookupError):
                                            continue
                            if runtime_uids(process) != credentials:
                                raise ScanRefused('credentials_changed')
                    except (FileNotFoundError, ProcessLookupError):
                        continue
    except OSError as error:
        # Never expose paths, process names, targets, payloads or raw errors.
        raise ScanRefused('metadata_unavailable') from None
    return True, counts


def root_directory(path):
    """Anchor an absolute directory without following any symlink component."""
    if (not isinstance(path, str) or not path.startswith('/')
            or any(piece in ('', '.', '..') for piece in path.split('/')[1:])):
        raise ScanRefused('path_schema')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for piece in path.split('/')[1:]:
            nxt = os.open(piece, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                          dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def credentials(connection):
    return struct.unpack('3i', connection.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))


def root_controlled_directory(path):
    current=Path(path)
    if not current.is_absolute():
        raise ScanRefused('trusted_path')
    for ancestor in (current,*current.parents):
        info=ancestor.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o022):
            raise ScanRefused('config_ownership')


def receive(connection):
    """Exactly one bounded JSON object per connection; no trailing frame."""
    raw = bytearray()
    while True:
        block = connection.recv(513-len(raw))
        if not block:
            break
        raw.extend(block)
        if len(raw) > 512:
            raise ScanRefused('message_bound')
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ScanRefused('message_schema') from None
    if type(value) is not dict:
        raise ScanRefused('message_schema')
    return value


def trusted_config(path):
    """Root-owned fixed target config. No caller-controlled target or UID."""
    parent, name = os.path.split(path)
    if not name or name in ('.','..'):
        raise ScanRefused('config_schema')
    # Every configuration ancestor must be root-controlled, not just the file.
    root_controlled_directory(parent)
    parent_fd = root_directory(parent)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                    or info.st_nlink != 1 or info.st_mode & 0o022):
                raise ScanRefused('config_ownership')
            data = os.read(fd, 4097)
            if len(data) > 4096:
                raise ScanRefused('config_bound')
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)
    value = json.loads(data)
    if (type(value) is not dict or set(value) != {'version','uid','archives'}
            or type(value['version']) is not int or value['version'] != 1
            or type(value['uid']) is not int or value['uid'] <= 0):
        raise ScanRefused('config_schema')
    # Opening the target is deferred until an authenticated peer asks.
    if not isinstance(value['archives'],str):
        raise ScanRefused('config_schema')
    return value


def target_identity(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_mode,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def answer(connection, config):
    """One authenticated metadata request; errors are opaque fixed responses."""
    connection.settimeout(7)
    try:
        if os.geteuid() != 0 or credentials(connection)[1] != config['uid']:
            raise ScanRefused('peer_identity')
        request = receive(connection)
        if (set(request) == {'version','probe'} and type(request['version']) is int
                and request['version'] == 1 and request['probe'] is True):
            readable, _ = scan_inode(config['uid'],0,1,readability_only=True)
            response = dict(version=1,ok=True,readable=readable)
            connection.sendall(json.dumps(response,separators=(',',':')).encode())
            return
        if (set(request) != {'version','device','inode'}
                or type(request['version']) is not int or request['version'] != 1
                or type(request['device']) is not int or request['device'] < 0
                or type(request['inode']) is not int or request['inode'] <= 0):
            raise ScanRefused('message_schema')
        archive = root_directory(config['archives'])
        try:
            parent = os.fstat(archive)
            if (parent.st_uid != config['uid']
                    or stat.S_IMODE(parent.st_mode) != 0o700):
                raise ScanRefused('target_ownership')
            info = os.stat('incoming.raw',dir_fd=archive,follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != config['uid']
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                    or (info.st_dev,info.st_ino) != (request['device'],request['inode'])):
                raise ScanRefused('target_identity')
            closed, _ = scan_inode(config['uid'],info.st_dev,info.st_ino)
            after = os.stat('incoming.raw',dir_fd=archive,follow_symlinks=False)
            if target_identity(after) != target_identity(info):
                raise ScanRefused('target_changed')
        finally:
            os.close(archive)
        response = dict(version=1,ok=True,closed=closed)
    except (OSError, ValueError, ScanRefused):
        response = dict(version=1,ok=False)
    connection.sendall(json.dumps(response,separators=(',',':')).encode())


def _request_remote(path, request, result_key):
    """Runtime-side required reader: missing/untrusted/malformed means refusal."""
    root_controlled_directory(str(Path(path).parent))
    before=Path(path).lstat()
    if not stat.S_ISSOCK(before.st_mode) or before.st_uid != 0:
        raise ScanRefused('server_identity')
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
        connection.settimeout(8)
        connection.connect(path)
        if credentials(connection)[1] != 0:
            raise ScanRefused('server_identity')
        after=Path(path).lstat()
        if (after.st_dev,after.st_ino) != (before.st_dev,before.st_ino):
            raise ScanRefused('server_identity')
        connection.sendall(json.dumps(request,separators=(',',':')).encode())
        connection.shutdown(socket.SHUT_WR)
        response = receive(connection)
    if (set(response) != {'version','ok',result_key}
            or type(response['version']) is not int or response['version'] != 1
            or response['ok'] is not True or type(response[result_key]) is not bool):
        raise ScanRefused('reader_refused')
    return response[result_key]


def check_remote(path, info):
    """Check only the configured incoming inode, never a caller-supplied path."""
    return _request_remote(path,dict(version=1,device=info.st_dev,inode=info.st_ino),'closed')


def probe_remote(path):
    """Readability observation only; creates no archive file or store lock."""
    return _request_remote(path,dict(version=1,probe=True),'readable')


def serve_activated(config_path):
    """One request from systemd's single inherited AF_UNIX listening socket."""
    if (os.geteuid() != 0 or os.environ.get('LISTEN_PID') != str(os.getpid())
            or os.environ.get('LISTEN_FDS') != '1'):
        raise ScanRefused('activation_identity')
    config = trusted_config(config_path)
    with socket.socket(fileno=3) as listener:
        if (listener.family != socket.AF_UNIX or listener.type != socket.SOCK_STREAM
                or listener.getsockopt(socket.SOL_SOCKET,socket.SO_ACCEPTCONN) != 1):
            raise ScanRefused('activation_socket')
        listener.settimeout(5)
        connection, _ = listener.accept()
        with connection:
            answer(connection,config)


if __name__ == '__main__':
    import argparse
    import sys
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',required=True)
    args=parser.parse_args()
    try:
        serve_activated(args.config)
    except Exception:
        print('INODE_GUARD_REFUSED',file=sys.stderr)
        sys.exit(1)
