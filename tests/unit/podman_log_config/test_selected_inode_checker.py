"""Selected closure boundary across native archive and operational recovery."""
import gzip
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from archive_store import ArchiveStore
from bounded_capture import BoundedFile, PendingWriter
from main import Component


class SelectedCheckerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.root.chmod(0o700)
        self.archives=self.root/'archives'
        self.archives.mkdir(mode=0o700)

    def tearDown(self):
        self.temp.cleanup()

    def test_selected_reader_pending_then_closed_compresses_same_input(self):
        checker=mock.Mock(return_value=False)
        store=ArchiveStore(self.archives,2,2,max_file=1024,inode_checker=checker)
        source=self.root/'source'; source.write_bytes(b'native-fixture')
        with store.locked(),mock.patch('archive_store.closed_inode',side_effect=AssertionError('fallback')):
            store.claim(source)
            self.assertIsNone(store.compress(()))
            self.assertEqual((self.archives/'incoming.raw').read_bytes(),b'native-fixture')
            self.assertFalse((self.archives/'incoming.gz.tmp').exists())
            checker.return_value=True
            result=store.compress(())
        self.assertEqual(gzip.decompress(result.read_bytes()),b'native-fixture')
        self.assertEqual(checker.call_count,2)

    def test_operational_recovery_cannot_bypass_required_reader(self):
        checker=mock.Mock(side_effect=ConnectionError('fixture reader unavailable'))
        store=ArchiveStore(self.archives,2,2,max_file=1024,inode_checker=checker)
        active=self.root/'operations.log'; active.write_bytes(b'operational-fixture')
        bounded=BoundedFile(store,active,lambda: ())
        with store.locked(),mock.patch('archive_store.closed_inode',side_effect=AssertionError('fallback')):
            with self.assertRaises(ConnectionError):
                bounded.rollover()
        self.assertEqual((self.archives/'incoming.raw').read_bytes(),b'operational-fixture')
        self.assertFalse(list(self.archives.glob('*.gz')))
        checker.side_effect=None; checker.return_value=False
        with store.locked(),self.assertRaises(PendingWriter):
            bounded.recover()
        checker.return_value=True
        with store.locked():
            bounded.recover()
        self.assertEqual(gzip.decompress(store.archives()[0][1].read_bytes()),b'operational-fixture')

    def test_nonboolean_reader_answer_never_allows_compression(self):
        store=ArchiveStore(self.archives,2,2,max_file=1024,inode_checker=lambda info,pids: 1)
        source=self.root/'source'; source.write_bytes(b'preserved')
        with store.locked():
            store.claim(source)
            with self.assertRaises(ValueError):
                store.compress(())
        self.assertEqual((self.archives/'incoming.raw').read_bytes(),b'preserved')

    def test_component_required_reader_owns_pid_selection(self):
        config=dict(user_uid=os.geteuid(),directory=str(self.root),operational=True,
                    inode_guard_socket='/run/podman-component-inode-guard/example.sock')
        with mock.patch('main.owned_pids',side_effect=AssertionError('incomplete PID enumeration')), \
                mock.patch('inode_guard.check_remote',side_effect=FileNotFoundError) as reader:
            component=Component(config)
            source=self.root/'source'; source.write_bytes(b'component-fixture')
            with component.store.locked():
                component.store.claim(source)
                with self.assertRaises(FileNotFoundError):
                    component.recover()
        reader.assert_called_once()
        self.assertEqual((self.archives/'incoming.raw').read_bytes(),b'component-fixture')


if __name__=='__main__':
    unittest.main()
