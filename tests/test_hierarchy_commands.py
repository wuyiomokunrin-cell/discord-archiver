"""Tests for Discord-style channel hierarchy (parent_id) and slash commands."""

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import discord

from archiver import commands
from archiver.capture import channel_parent_id
from archiver.db import Database
from archiver.listener import ArchiverClient


class TestChannelParent(unittest.TestCase):
    def test_thread_parent_recorded(self):
        th = SimpleNamespace(type=SimpleNamespace(value=11), parent_id=4242)
        self.assertEqual(channel_parent_id(th), "4242")

    def test_non_thread_parent_none(self):
        ch = SimpleNamespace(type=SimpleNamespace(value=0), parent_id=99)
        self.assertIsNone(channel_parent_id(ch))

    def test_parent_id_stored_and_read(self):
        with TemporaryDirectory() as d:
            db = Database(Path(d) / "a.sqlite3")
            db.upsert_guild(1, "G")
            db.upsert_channel(10, 1, "forum", type_=15, position=0)
            db.upsert_channel(11, 1, "thread", type_=11, position=0, parent_id=10)
            row = db.conn.execute(
                "SELECT parent_id FROM channels WHERE id='11'").fetchone()
            db.close()
            self.assertEqual(row["parent_id"], "10")

    def test_legacy_db_gains_parent_id_column(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "legacy.sqlite3"
            con = sqlite3.connect(p)
            con.execute(
                "CREATE TABLE channels(id TEXT PRIMARY KEY, guild_id TEXT, "
                "name TEXT, type INTEGER, position INTEGER, category_id TEXT, "
                "topic TEXT, nsfw INTEGER, first_seen TEXT, updated_at TEXT)")
            con.commit(); con.close()
            db = Database(p)  # runs the migration
            cols = [r[1] for r in db.conn.execute("PRAGMA table_info(channels)")]
            db.close()
            self.assertIn("parent_id", cols)


class TestSlashCommands(unittest.TestCase):
    def _client(self, db):
        return ArchiverClient(db, intents=discord.Intents.default())

    def test_group_registered_with_subcommands(self):
        with TemporaryDirectory() as d:
            client = self._client(Database(Path(d) / "a.sqlite3"))
            group = client.tree.get_command("archive")
            self.assertIsNotNone(group)
            names = {c.name for c in group.commands}
            self.assertSetEqual(
                names, {"help", "stats", "channels", "members", "roles",
                        "audit", "search"})

    def test_group_can_bind_to_guild_for_sync(self):
        # A guild sync only uploads guild-bound commands; the client must expose
        # the group so sync() can bind it, otherwise the sync is empty.
        with TemporaryDirectory() as d:
            client = self._client(Database(Path(d) / "a.sqlite3"))
            self.assertEqual(client.archive_group.name, "archive")
            g = discord.Object(id=999)
            client.tree.add_command(client.archive_group, guild=g, override=True)
            bound = client.tree._get_all_commands(guild=g)
            self.assertEqual([c.name for c in bound], ["archive"])

    def test_is_mod_gate(self):
        admin = SimpleNamespace(guild_permissions=discord.Permissions(
            administrator=True))
        pleb = SimpleNamespace(guild_permissions=discord.Permissions())
        self.assertTrue(commands._is_mod(SimpleNamespace(user=admin)))
        self.assertFalse(commands._is_mod(SimpleNamespace(user=pleb)))


if __name__ == "__main__":
    unittest.main()
