"""Real Linux FIFO/inode integration with a synthetic writer, not conmon acceptance."""

import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from main import Component, load_config


WRITER = '''import os,sys
from pathlib import Path
def value(key): return sys.argv[sys.argv.index(key)+1]
path=Path(value('--log-path').split(':',1)[1])
fifo=Path(value('--bundle'))/'ctl'
os.mkfifo(fifo,0o600)
current=path.open('wb',buffering=0)
current.write(b'native-before-daily')
with os.fdopen(os.open(fifo,os.O_RDWR),'r') as control:
    for line in control:
        if line!='2 0 0\\n': raise SystemExit(33)
        closed=Path(str(path)+'.1')
        if closed.exists(): closed.rename(Path(str(path)+'.2'))
        path.rename(closed)
        replacement=path.open('wb',buffering=0)
        current.close()
        current=replacement
        current.write(b'native-after-daily')
'''


class NativeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.logs = self.root / 'logs'
        self.logs.mkdir(mode=0o700)
        (self.logs / 'archives').mkdir(mode=0o700)
        self.cid = 'a' * 64
        graph = self.root / 'storage'
        self.active = graph / 'overlay-containers' / self.cid / 'userdata' / 'ctr.log'
        self.active.parent.mkdir(parents=True)
        runtime = self.root / 'runtime'
        self.bundle = runtime / self.cid
        self.bundle.mkdir(parents=True)
        self.config = dict(version=1, user_uid=os.geteuid(), component='example',
                           container='example-container', directory=str(self.logs),
                           graphroot=str(graph), runtime_root=str(runtime),
                           conmon=str(Path(sys.executable).resolve()), podman='/usr/bin/podman',
                           operational=True)
        self.script = self.root / 'writer.py'
        self.script.write_text(WRITER)
        self.child = None

    def tearDown(self):
        if self.child is not None:
            if self.child.poll() is None:
                self.child.terminate()
            self.child.wait(timeout=5)
        self.temp.cleanup()

    def start(self, size=104857600):
        self.child = subprocess.Popen([sys.executable, str(self.script), '--cid', self.cid,
                                       '--bundle', str(self.bundle), '--log-path', 'k8s-file:' + str(self.active),
                                       '--log-size-max=' + str(size), '--log-rotate', '--log-max-files=2'])
        import time
        deadline = time.monotonic() + 5
        while not self.active.exists():
            if time.monotonic() > deadline or self.child.poll() is not None:
                self.fail('synthetic native writer failed to start')
            time.sleep(0.01)

    def result(self):
        return subprocess.CompletedProcess([], 0, json.dumps(dict(
            id=self.cid, pid=self.child.pid, running=True,
            log=dict(Type='k8s-file', Path=str(self.active), Size='100MB'))).encode(), b'')

    def response(self, command, **kwargs):
        if command[-1] == '--version':
            return subprocess.CompletedProcess(command, 0, b'conmon version 2.2.1\n', b'')
        return self.result()

    def test_real_fifo_daily_inode_change_compression_and_state_idempotence(self):
        self.start()
        component = Component(self.config)
        with mock.patch('main.subprocess.run', side_effect=self.response):
            self.assertTrue(component.collect())
            after = self.active.stat().st_ino
            self.assertEqual(self.active.read_bytes(), b'native-after-daily')
            archives = component.store.archives()
            self.assertEqual(len(archives), 1)
            self.assertEqual(gzip.decompress(archives[0][1].read_bytes()), b'native-before-daily')
            self.assertTrue(component.collect())
            self.assertEqual(self.active.stat().st_ino, after)
            self.assertEqual(len(component.store.archives()), 1)

    def test_native_size_argument_is_exact_even_when_inspect_text_is_rounded(self):
        self.start(size=104857599)
        component = Component(self.config)
        with mock.patch('main.subprocess.run', side_effect=self.response):
            with self.assertRaises(ValueError):
                component.inspect()
        self.assertFalse(component.state.exists())

    def test_foreign_container_log_path_is_rejected(self):
        self.start()
        response = self.result()
        payload = json.loads(response.stdout)
        payload['id'] = 'b' * 64
        response.stdout = json.dumps(payload).encode()
        def changed(command, **kwargs):
            return self.response(command, **kwargs) if command[-1] == '--version' else response

        with mock.patch('main.subprocess.run', side_effect=changed):
            with self.assertRaises(ValueError):
                Component(self.config).inspect()

    def test_unverified_native_version_is_rejected(self):
        response = subprocess.CompletedProcess([], 0, b'conmon version 9.0.0\n', b'')
        with mock.patch('main.subprocess.run', return_value=response), self.assertRaises(ValueError):
            Component(self.config).inspect()

    def test_configuration_rejects_runtime_mismatch_and_unknown_fields(self):
        config = self.root / 'config.json'
        config.write_text(json.dumps(self.config))
        with self.assertRaises(ValueError):
            load_config(config)
        valid = dict(self.config, runtime_root='/run/user/' + str(os.geteuid()))
        config.write_text(json.dumps(valid))
        self.assertEqual(load_config(config)['component'], 'example')
        valid['unexpected'] = 'rejected'
        config.write_text(json.dumps(valid))
        with self.assertRaises(ValueError):
            load_config(config)


if __name__ == '__main__':
    unittest.main(verbosity=2)
