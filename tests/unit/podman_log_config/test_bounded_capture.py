import datetime
import gzip
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from archive_store import ArchiveStore
from bounded_capture import BoundedFile, PendingWriter, capture


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.archives = self.root / "archives"
        self.archives.mkdir(mode=0o700)
        self.store = ArchiveStore(self.archives, native_archives=2, active_files=2, max_file=65536)
        self.active = self.root / "operations.log"
        self.writer = BoundedFile(self.store, self.active, lambda: ())

    def tearDown(self):
        self.temp.cleanup()

    def all_payload(self):
        items = self.store.archives()
        return b"".join(gzip.decompress(p.read_bytes()) for _, p in items) + self.active.read_bytes()

    def test_real_child_combined_stream_and_failure_status(self):
        command = [sys.executable, "-c", "import os;os.write(1,b'OUT');os.write(2,b'ERR');raise SystemExit(17)"]
        self.assertEqual(capture(command, self.writer), 17)
        self.assertEqual(self.active.read_bytes(), b"OUTERR")

    def test_large_incompressible_chunks_have_no_loss_or_overshoot(self):
        payload = os.urandom(3 * 65536 + 567)
        with self.store.locked():
            self.writer.write(payload)
        self.assertEqual(self.active.stat().st_size, 567)
        self.assertEqual(len(self.store.archives()), 3)
        self.assertEqual(self.all_payload(), payload)

    def test_quiet_daily_rollover_and_restart(self):
        start = datetime.datetime(2026, 11, 10, 12, tzinfo=datetime.timezone.utc).timestamp()
        now = [start]
        self.writer.clock = lambda: now[0]
        with self.store.locked():
            self.writer.write(b"before midnight")
            now[0] += 86400
            self.writer.tick()
            self.assertFalse(self.active.exists())
            self.writer.write(b"after midnight")
        restarted = BoundedFile(self.store, self.active, lambda: (), clock=lambda: now[0])
        with self.store.locked():
            restarted.write(b"after restart")
        self.assertEqual(self.active.read_bytes(), b"after restart")
        self.assertEqual(len(self.store.archives()), 2)

    def test_more_than_seven_rotations_still_reserves_native_and_work_slots(self):
        with self.store.locked():
            self.writer.write(b"x" * 20 * 65536)
        self.assertEqual(len(self.store.archives()), 3)
        self.assertLessEqual(self.active.stat().st_size, 65536)

    def test_foreign_store_prevents_child_start(self):
        (self.archives / "foreign.dump").write_bytes(b"preserve")
        with mock.patch("bounded_capture.subprocess.Popen") as child:
            with self.assertRaises(ValueError):
                capture([sys.executable, "-c", "raise SystemExit(0)"], self.writer)
        child.assert_not_called()

    def test_capture_write_failure_reaps_child(self):
        real_popen = __import__("subprocess").Popen
        started = []

        def tracked(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            started.append(child)
            return child

        with mock.patch("bounded_capture.subprocess.Popen", tracked):
            with mock.patch.object(self.writer, "write", side_effect=OSError("simulated disk failure")):
                with self.assertRaises(OSError):
                    capture([sys.executable, "-c", "import os,time;os.write(1,b'log');time.sleep(60)"], self.writer)
        self.assertEqual(len(started), 1)
        self.assertIsNotNone(started[0].poll())

    def test_partial_chunk_is_not_replayed_on_pending_writer(self):
        attempts = []

        for error in (PendingWriter, BlockingIOError):
            with self.subTest(error=error):
                attempts.clear()

                def interrupted(data):
                    attempts.append(data)
                    self.active.write_bytes(data[:2])
                    raise error("simulated interrupted rollover after partial write")

                with mock.patch.object(self.writer, "write", interrupted):
                    with self.assertRaises(error):
                        capture([sys.executable, "-c", "import os;os.write(1,b'abcdef')"], self.writer)
                self.assertEqual(attempts, [b"abcdef"])
                self.assertEqual(self.active.read_bytes(), b"ab")

    def test_actual_100_mib_boundary_with_incompressible_archive(self):
        store = ArchiveStore(self.archives, native_archives=2, active_files=2)
        writer = BoundedFile(store, self.active, lambda: ())
        digest = hashlib.sha256()
        with store.locked():
            for _ in range(100):
                block = os.urandom(1024**2)
                digest.update(block)
                writer.write(block)
            self.assertEqual(self.active.stat().st_size, 100 * 1024**2)
            writer.write(b"next active file")
        self.assertEqual(self.active.read_bytes(), b"next active file")
        archives = store.archives()
        self.assertEqual(len(archives), 1)
        actual = hashlib.sha256()
        with gzip.open(archives[0][1], "rb") as restored:
            for block in iter(lambda: restored.read(1024**2), b""):
                actual.update(block)
        self.assertEqual(actual.digest(), digest.digest())
        self.assertGreater(archives[0][1].stat().st_size, 100 * 1024**2)
        self.assertLessEqual(archives[0][1].stat().st_size, store.gzip_limit)


if __name__ == "__main__":
    unittest.main(verbosity=2)
