"""Synthetic Linux filesystem/failure checks. No application acceptance claim."""

import gzip
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from archive_store import ArchiveStore


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.logs = self.base / "archives"
        self.logs.mkdir(mode=0o700)
        self.store = ArchiveStore(self.logs, native_archives=2, active_files=2, max_file=65536)

    def tearDown(self):
        self.temp.cleanup()

    def feed(self, content):
        source = self.base / "native.log.1"
        source.write_bytes(content)
        self.store.claim(source)
        return self.store.compress(())

    def test_binary_roundtrip_and_permissions(self):
        payload = os.urandom(65536)
        with self.store.locked():
            result = self.feed(payload)
        self.assertEqual(gzip.decompress(result.read_bytes()), payload)
        self.assertEqual(result.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.logs / "incoming.raw").exists())
        self.assertFalse((self.logs / "incoming.gz.tmp").exists())

    def test_joint_ceiling_during_every_compression(self):
        original = gzip.GzipFile.write
        samples = []

        def observed_write(stream, data):
            # Reserve two independently rotating native archives. Exclude lock.
            count = len([p for p in self.logs.iterdir() if p.name != "store.lock"]) + 2
            samples.append(count)
            return original(stream, data)

        with self.store.locked(), mock.patch.object(gzip.GzipFile, "write", observed_write):
            for index in range(20):
                self.feed(("sequence-%d" % index).encode() * 200)
        self.assertTrue(samples)
        self.assertLessEqual(max(samples), 7)
        self.assertEqual(len(self.store.archives()), 3)
        payloads = [gzip.decompress(p.read_bytes()) for _, p in self.store.archives()]
        self.assertIn(b"sequence-19" * 200, payloads)

    def test_incompressible_storage_budget_for_all_sources(self):
        for natives in (0, 1, 2):
            other = self.base / ("budget-%d" % natives)
            other.mkdir(mode=0o700)
            # Worst-case reservation is checked without allocating gigabytes.
            store = ArchiveStore(other, native_archives=natives, active_files=2)
            self.assertGreaterEqual(store.published_limit, 3)
        with self.assertRaises(ValueError):
            ArchiveStore(self.logs, native_archives=2, active_files=2, max_total=100)

    def test_configuration_cannot_expand_approved_limits(self):
        for options in ({"max_file": 0}, {"max_file": 100 * 1024**2 + 1},
                        {"max_archives": 8}, {"max_total": 1024**3 + 1},
                        {"max_age": 14 * 86400 + 1}, {"max_file": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                ArchiveStore(self.logs, 2, 2, **options)

    def test_hardlinked_input_is_not_claimed(self):
        backup = self.base / "backup.dump"
        source = self.base / "native.log.1"
        backup.write_bytes(b"retain backup")
        os.link(backup, source)
        with self.store.locked(), self.assertRaises(ValueError):
            self.store.claim(source)
        self.assertEqual(backup.read_bytes(), b"retain backup")
        self.assertTrue(source.exists())

    def test_expired_archives_pruned_without_touching_backups(self):
        backup = self.base / "database.dump"
        backup.write_bytes(b"BACKUP_DATA_IS_OUTSIDE_STORE")
        with self.store.locked():
            old = self.feed(b"old log")
            now = time.time()
            os.utime(old, (now - 14 * 86400 - 1, now - 14 * 86400 - 1))
            self.store.prune(now)
        self.assertFalse(old.exists())
        self.assertEqual(backup.read_bytes(), b"BACKUP_DATA_IS_OUTSIDE_STORE")

    def test_crash_after_output_fsync_recovers_raw(self):
        source = self.base / "native.log.1"
        payload = os.urandom(1024)
        source.write_bytes(payload)
        with self.store.locked():
            self.store.claim(source)
            with mock.patch("archive_store.os.rename", side_effect=OSError("simulated publish failure")):
                with self.assertRaises(OSError):
                    self.store.compress(())
            self.assertEqual((self.logs / "incoming.raw").read_bytes(), payload)
            self.assertTrue((self.logs / "incoming.gz.tmp").exists())
        recovered = ArchiveStore(self.logs, 2, 2, max_file=65536)
        with recovered.locked():
            result = recovered.compress(())
        self.assertEqual(gzip.decompress(result.read_bytes()), payload)

    def test_disk_write_failure_preserves_input(self):
        source = self.base / "native.log.1"
        source.write_bytes(b"must survive")
        with self.store.locked():
            self.store.claim(source)
            with mock.patch.object(gzip.GzipFile, "write", side_effect=OSError("simulated ENOSPC")):
                with self.assertRaises(OSError):
                    self.store.compress(())
            self.assertEqual((self.logs / "incoming.raw").read_bytes(), b"must survive")
            self.assertIsNotNone(self.store.compress(()))

    def test_crash_after_publish_recovers_without_duplicate_archive(self):
        source = self.base / "native.log.1"
        source.write_bytes(b"publish exactly once")
        unlink = Path.unlink

        def interrupted_unlink(path, *args, **kwargs):
            if path == self.logs / "incoming.raw":
                raise OSError("simulated interruption after publish")
            return unlink(path, *args, **kwargs)

        with self.store.locked():
            self.store.claim(source)
            with mock.patch.object(Path, "unlink", interrupted_unlink):
                with self.assertRaises(OSError):
                    self.store.compress(())
            self.assertEqual(len(self.store.archives()), 1)
            recovered = self.store.compress(())
            self.assertEqual(len(self.store.archives()), 1)
            self.assertEqual(gzip.decompress(recovered.read_bytes()), b"publish exactly once")

    def test_second_collector_cannot_acquire_store(self):
        with self.store.locked():
            with self.assertRaises(BlockingIOError):
                with self.store.locked():
                    self.fail("concurrent lock acquired")

    def test_symlink_and_foreign_files_fail_closed(self):
        victim = self.base / "backup.dump"
        victim.write_bytes(b"do not modify")
        link = self.logs / "incoming.gz.tmp"
        link.symlink_to(victim)
        with self.store.locked(), self.assertRaises(ValueError):
            self.store.compress(())
        self.assertEqual(victim.read_bytes(), b"do not modify")
        link.unlink()
        (self.logs / "foreign.log").write_bytes(b"foreign")
        with self.store.locked(), self.assertRaises(ValueError):
            self.store.prune()
        self.assertEqual((self.logs / "foreign.log").read_bytes(), b"foreign")

    def test_oversize_and_second_claim_rejected(self):
        source = self.base / "native.log.1"
        source.write_bytes(b"x" * 65537)
        with self.store.locked():
            with self.assertRaises(ValueError):
                self.store.claim(source)
            self.assertTrue(source.exists())
            source.write_bytes(b"first")
            self.store.claim(source)
            source.write_bytes(b"second")
            with self.assertRaises(FileExistsError):
                self.store.claim(source)
            self.assertEqual(source.read_bytes(), b"second")

    def test_real_open_writer_inode_exclusion(self):
        source = self.base / "native.log.1"
        active = self.base / "native.log"
        worker = (
            "import os,sys\n"
            "old=open(sys.argv[1],'wb',buffering=0)\n"
            "old.write(b'closed-archive-candidate')\n"
            "print('OPEN',flush=True)\n"
            "sys.stdin.readline()\n"
            "old.close()\n"
            "new=open(sys.argv[2],'wb',buffering=0)\n"
            "new.write(b'new-active-writer')\n"
            "print('CLOSED',flush=True)\n"
            "sys.stdin.readline()\n"
        )
        process = subprocess.Popen([sys.executable, "-c", worker, str(source), str(active)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "OPEN")
            with self.store.locked():
                self.store.claim(source)
                self.assertIsNone(self.store.compress([process.pid]))
                process.stdin.write("close\n")
                process.stdin.flush()
                self.assertEqual(process.stdout.readline().strip(), "CLOSED")
                result = self.store.compress([process.pid])
                self.assertEqual(gzip.decompress(result.read_bytes()), b"closed-archive-candidate")
                self.assertEqual(active.read_bytes(), b"new-active-writer")
        finally:
            process.stdin.close()
            process.wait(timeout=5)
            process.stdout.close()

    def test_unreadable_live_writer_metadata_fails_closed(self):
        source = self.base / "native.log.1"
        source.write_bytes(b"retain until writer status can be verified")
        original = Path.iterdir
        target = Path('/proc') / str(os.getpid()) / 'fd'

        def denied(path):
            if path == target:
                raise PermissionError('synthetic denial for a live process')
            return original(path)

        with self.store.locked():
            self.store.claim(source)
            with mock.patch.object(Path, 'iterdir', denied), self.assertRaises(PermissionError):
                self.store.compress([os.getpid()])
            self.assertTrue((self.logs / 'incoming.raw').exists())
            self.assertEqual(len(self.store.archives()), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
