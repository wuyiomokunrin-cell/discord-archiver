"""Tests for configuration loading and validation."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from archiver.config import Config, load, load_dotenv


class TestDotenv(unittest.TestCase):
    def test_parses_and_strips_quotes(self):
        with TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text(
                '# comment\n'
                'FOO=bar\n'
                'QUOTED="hello world"\n'
                "SINGLE='x'\n"
                '\n'
                'EMPTY=\n'
            )
            for k in ("FOO", "QUOTED", "SINGLE", "EMPTY"):
                os.environ.pop(k, None)
            load_dotenv(p)
            self.assertEqual(os.environ["FOO"], "bar")
            self.assertEqual(os.environ["QUOTED"], "hello world")
            self.assertEqual(os.environ["SINGLE"], "x")
            self.assertEqual(os.environ["EMPTY"], "")

    def test_does_not_override_existing_env(self):
        with TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text("ARCHIVER_TEST_KEY=from_file\n")
            os.environ["ARCHIVER_TEST_KEY"] = "from_env"
            load_dotenv(p)
            self.assertEqual(os.environ["ARCHIVER_TEST_KEY"], "from_env")

    def test_missing_file_is_a_noop(self):
        load_dotenv("/nonexistent/.env")  # must not raise


class TestValidate(unittest.TestCase):
    def _cfg(self, token="", guild=None):
        with TemporaryDirectory() as d:
            os.environ["ARCHIVER_DATA_DIR"] = d
            os.environ["DISCORD_BOT_TOKEN"] = token
            if guild:
                os.environ["DISCORD_GUILD_ID"] = str(guild)
            else:
                os.environ.pop("DISCORD_GUILD_ID", None)
            return Config()

    def test_missing_token_and_guild_reported(self):
        problems = self._cfg().validate(need_token=True, need_guild=True)
        self.assertEqual(len(problems), 2)
        self.assertTrue(any("DISCORD_BOT_TOKEN" in p for p in problems))
        self.assertTrue(any("DISCORD_GUILD_ID" in p for p in problems))

    def test_offline_command_does_not_require_credentials(self):
        """`stats` reads the local db only - it must not demand a token."""
        self.assertEqual(
            self._cfg().validate(need_token=False, need_guild=False), [])

    def test_valid_config_passes(self):
        self.assertEqual(
            self._cfg(token="abc", guild=111).validate(), [])

    def test_directories_are_created(self):
        # mkdtemp rather than TemporaryDirectory: the context manager would
        # delete the tree before these assertions get to look at it.
        import shutil
        import tempfile
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        os.environ["ARCHIVER_DATA_DIR"] = d
        os.environ["DISCORD_BOT_TOKEN"] = "abc"
        os.environ["DISCORD_GUILD_ID"] = "111"
        cfg = Config()
        for path in (cfg.data_dir, cfg.attachments_dir, cfg.exports_dir):
            self.assertTrue(path.is_dir(), path)
        self.assertTrue(str(cfg.db_path).endswith("archive.sqlite3"))
        self.assertEqual(str(cfg.data_dir), d)


class TestBooleans(unittest.TestCase):
    def test_truthy_strings(self):
        with TemporaryDirectory() as d:
            os.environ["ARCHIVER_DATA_DIR"] = d
            for val, expected in [("true", True), ("1", True), ("YES", True),
                                  ("on", True), ("false", False), ("0", False),
                                  ("no", False)]:
                os.environ["DOWNLOAD_ATTACHMENTS"] = val
                self.assertIs(Config().download_attachments, expected, val)


if __name__ == "__main__":
    unittest.main()
