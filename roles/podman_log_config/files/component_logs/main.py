#!/usr/bin/python3
"""Rootless component log lifecycle. Never emits command output or exceptions."""

import argparse
import datetime
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

from archive_store import ArchiveStore, RAW, regular
from bounded_capture import BoundedFile, capture


FORMAT = ('{"id":{{json .ID}},"pid":{{json .State.ConmonPid}},'
          '"running":{{json .State.Running}},"log":{{json .HostConfig.LogConfig}}}')


def canonical(value):
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("noncanonical path")
    return path


def load_config(path):
    path = canonical(path)
    info = regular(path)
    if info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022 or info.st_size > 16384:
        raise ValueError("unsafe configuration")
    value = json.loads(path.read_text())
    required = {"version", "user_uid", "component", "directory", "container", "graphroot",
                "runtime_root", "conmon", "podman", "operational"}
    if (not isinstance(value, dict) or set(value) not in (required, required | {"inode_guard_socket"})
            or value["version"] != 1):
        raise ValueError("invalid configuration schema")
    if type(value["user_uid"]) is not int or value["user_uid"] <= 0:
        raise ValueError("requires an unprivileged runtime user")
    for field in ("component", "container"):
        if not isinstance(value[field], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", value[field]):
            raise ValueError("invalid identifier")
    if type(value["operational"]) is not bool:
        raise ValueError("invalid operational log flag")
    for field in ("directory", "graphroot", "runtime_root", "conmon", "podman"):
        canonical(value[field])
    if Path(value["runtime_root"]) != Path("/run/user") / str(value["user_uid"]):
        raise ValueError("runtime directory identity mismatch")
    if "inode_guard_socket" in value:
        if not isinstance(value["inode_guard_socket"], str) or len(value["inode_guard_socket"].encode()) > 107:
            raise ValueError("invalid inode guard socket")
        canonical(value["inode_guard_socket"])
    return value


def option(arguments, *names):
    values = []
    for index, argument in enumerate(arguments):
        for name in names:
            if argument == name and index + 1 < len(arguments):
                values.append(arguments[index + 1])
            elif argument.startswith(name + "="):
                values.append(argument[len(name) + 1:])
    if len(values) != 1:
        raise ValueError("ambiguous native writer option")
    return values[0]


def owned_pids():
    """Include old/replaced same-user writers when recovering a pending inode."""
    result = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdecimal():
            try:
                if entry.stat().st_uid == os.geteuid():
                    result.append(int(entry.name))
            except FileNotFoundError:
                continue
    return result


class Component:
    def __init__(self, config):
        if os.geteuid() != config["user_uid"]:
            raise ValueError("runtime identity mismatch")
        self.config = config
        checker = None
        self.writer_pids = owned_pids
        if "inode_guard_socket" in config:
            from inode_guard import check_remote
            checker = lambda info, pids: check_remote(config["inode_guard_socket"], info)
            # The required reader owns complete PID/credential selection.
            self.writer_pids = lambda: ()
        self.root = canonical(config["directory"])
        info = self.root.stat()
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("unsafe component directory")
        self.store = ArchiveStore(self.root / "archives", native_archives=2,
                                  active_files=2 if config["operational"] else 1,
                                  inode_checker=checker)
        self.state = self.root / "native-state.json"

    def inspect(self):
        version = subprocess.run([self.config["conmon"], "--version"], capture_output=True,
                                 timeout=5, check=False)
        if version.returncode or version.stdout.splitlines()[:1] != [b"conmon version 2.2.1"]:
            raise ValueError("unsupported native log implementation version")
        result = subprocess.run([self.config["podman"], "inspect", "--format", FORMAT,
                                 self.config["container"]], capture_output=True, timeout=15, check=False)
        if result.returncode or len(result.stdout) > 16384:
            raise ValueError("native container inspection failed")
        model = json.loads(result.stdout)
        if not re.fullmatch(r"[0-9a-f]{64}", model["id"]):
            raise ValueError("invalid container identity")
        log = model["log"]
        if log["Type"] != "k8s-file":
            raise ValueError("unsupported native log driver")
        active = canonical(log["Path"])
        active.relative_to(canonical(self.config["graphroot"]))
        if (active.name != "ctr.log" or active.parent.name != "userdata"
                or active.parent.parent.name != model["id"]):
            raise ValueError("native log path is not bound to the inspected container")
        if active.stat().st_dev != self.root.stat().st_dev:
            raise ValueError("cross-filesystem archive claim unsupported")
        model["active"] = active
        if model["running"]:
            pid = model["pid"]
            if type(pid) is not int or pid <= 1:
                raise ValueError("invalid native PID")
            proc = Path("/proc") / str(pid)
            if proc.stat().st_uid != os.geteuid() or (proc / "exe").resolve() != canonical(self.config["conmon"]):
                raise ValueError("native writer identity mismatch")
            arguments = (proc / "cmdline").read_bytes().decode().strip("\0").split("\0")
            if (arguments[-2:] != ["--log-rotate", "--log-max-files=2"]
                    or option(arguments, "-c", "--cid") != model["id"]
                    or int(option(arguments, "--log-size-max")) != self.store.max_file):
                raise ValueError("native rotation policy is not active")
            if option(arguments, "-l", "--log-path") != "k8s-file:" + str(active):
                raise ValueError("native log writer path mismatch")
            bundle = canonical(option(arguments, "-b", "--bundle"))
            bundle.relative_to(canonical(self.config["runtime_root"]))
            model["control"] = bundle / "ctl"
        return model

    def recover(self):
        self.store.compress(self.writer_pids())
        return not (self.store.root / RAW).exists()

    def collect_archives(self, model):
        if not self.recover():
            return False
        for index in (2, 1):
            source = Path(str(model["active"]) + "." + str(index))
            try:
                self.store.claim(source)
            except FileNotFoundError:
                continue  # native rotation can rename this candidate concurrently
            if not self.recover():
                return False
        return True

    def daily(self, model):
        today = datetime.datetime.now().astimezone().date().isoformat()
        desired = {"id": model["id"], "day": today}
        if self.state.exists():
            regular(self.state)
            if json.loads(self.state.read_text()) == desired:
                return
        if not model["running"]:
            return  # do not stamp a rotation that was not performed
        active = model["active"]
        before = regular(active)
        if before.st_size:
            control = model["control"]
            info = control.lstat()
            if not stat.S_ISFIFO(info.st_mode) or info.st_uid != os.geteuid():
                raise ValueError("unsafe native control FIFO")
            fd = os.open(control, os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            try:
                if os.fstat(fd).st_ino != info.st_ino:
                    raise ValueError("native control FIFO changed")
                if os.write(fd, b"2 0 0\n") != 6:
                    raise OSError("native reopen request incomplete")
            finally:
                os.close(fd)
            deadline = time.monotonic() + 5
            while True:
                try:
                    if regular(active).st_ino != before.st_ino:
                        break
                except FileNotFoundError:
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError("native daily rotation was not observed")
                time.sleep(0.02)
        temporary = self.root / "native-state.tmp"
        if temporary.exists():
            regular(temporary)
            temporary.unlink()
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(desired, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self.state)
        self.store.sync(self.root)

    def collect(self):
        with self.store.locked():
            self.store.prune()
            model = self.inspect()
            if not self.collect_archives(model):
                return False
            self.daily(model)
            if not self.collect_archives(model):
                return False
            active = self.root / "operations.log"
            if self.config["operational"] and active.exists():
                before = datetime.datetime.fromtimestamp(regular(active).st_mtime).astimezone().date()
                if before != datetime.datetime.now().astimezone().date():
                    BoundedFile(self.store, active, self.writer_pids).rollover()
            self.store.prune()
            return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--validate", action="store_true")
    group.add_argument("--collect", action="store_true")
    group.add_argument("--capture", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if args.validate:
            print("COMPONENT_LOG_CONFIG_VALID")
            return 0
        component = Component(config)
        if args.collect:
            complete = component.collect()
            print("COMPONENT_LOG_COLLECT_OK" if complete else "COMPONENT_LOG_COLLECT_PENDING")
            return 0
        if not config["operational"] or not args.capture or not Path(args.capture[0]).is_absolute():
            raise ValueError("capture requires an explicit absolute command")
        component.inspect()
        return capture(args.capture, BoundedFile(component.store, component.root / "operations.log", component.writer_pids))
    except Exception:
        print("COMPONENT_LOG_OPERATION_FAILED", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
