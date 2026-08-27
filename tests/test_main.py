"""Tests for the entry-point helpers: the chunk timeout and the run lock."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import main as cli
from archiver import lock


class FakeGuild:
    def __init__(self, *, hang=False, members=(), raises=None):
        self.name = "Test Server"
        self.members = list(members)
        self._hang = hang
        self._raises = raises

    async def chunk(self):
        if self._raises:
            raise self._raises
        if self._hang:
            await asyncio.sleep(3600)  # never returns within a test
        return True


class TestChunkTimeout(unittest.TestCase):
    """A hanging chunk() must not block the archive. This is what stalled the
    real backfill after printing only 'connected'."""

    def setUp(self):
        self._orig = cli.CHUNK_TIMEOUT
        cli.CHUNK_TIMEOUT = 0.2

    def tearDown(self):
        cli.CHUNK_TIMEOUT = self._orig

    def test_hanging_chunk_times_out_instead_of_blocking(self):
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                asyncio.wait_for(cli._chunk_guild(FakeGuild(hang=True)), timeout=5))
        finally:
            loop.close()
        # Reaching this line at all is the assertion: no timeout means it returned.

    def test_working_chunk_completes(self):
        g = FakeGuild(members=[1, 2, 3])
        asyncio.run(cli._chunk_guild(g))

    def test_raising_chunk_is_swallowed(self):
        asyncio.run(cli._chunk_guild(FakeGuild(raises=RuntimeError("no intent"))))


class TestRunLock(unittest.TestCase):
    def test_second_process_is_refused(self):
        with TemporaryDirectory() as d:
            db = Path(d) / "archive.sqlite3"
            first = lock.acquire(db)
            try:
                with self.assertRaises(SystemExit) as ctx:
                    lock.acquire(db)
                self.assertIn("already using this database", str(ctx.exception))
            finally:
                first.close()

    def test_lock_is_reusable_after_release(self):
        """flock releases on close, so a crash cannot strand the lock."""
        with TemporaryDirectory() as d:
            db = Path(d) / "archive.sqlite3"
            first = lock.acquire(db)
            first.close()
            second = lock.acquire(db)  # must not raise
            second.close()

    def test_lock_file_holds_the_pid(self):
        import os
        with TemporaryDirectory() as d:
            db = Path(d) / "archive.sqlite3"
            fh = lock.acquire(db)
            try:
                self.assertEqual(Path(str(db) + ".lock").read_text(), str(os.getpid()))
            finally:
                fh.close()


class TestFormatDuration(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(cli._fmt_duration(45), "45s")

    def test_minutes_and_seconds(self):
        self.assertEqual(cli._fmt_duration(125), "2m 5s")

    def test_hours(self):
        self.assertEqual(cli._fmt_duration(3725), "1h 2m 5s")

    def test_zero(self):
        self.assertEqual(cli._fmt_duration(0), "0s")


if __name__ == "__main__":
    unittest.main()
