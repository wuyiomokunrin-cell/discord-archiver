"""Tests for message capture and the export layer."""

from __future__ import annotations

import csv
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from archiver import export as ex
from archiver.attachments import safe_filename, sha256_of, shard_path
from archiver.capture import capture_message, message_to_row
from archiver.db import Database

from .stubs import FakeAttachment, FakeAuthor, FakeChannel, FakeEmbed, FakeGuild, make_message


class TestCapture(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.db.upsert_guild(111, "Test Server")

    def tearDown(self):
        self.db.close()

    def test_capture_creates_channel_and_member_on_the_fly(self):
        """A message arriving before its channel is catalogued must not break FKs."""
        msg = make_message(1000)
        self.assertTrue(capture_message(self.db, msg))

        self.assertIsNotNone(self.db.conn.execute(
            "SELECT * FROM channels WHERE id='222'").fetchone())
        self.assertIsNotNone(self.db.conn.execute(
            "SELECT * FROM members WHERE id='333'").fetchone())
        self.assertEqual(self.db.stats()["messages"], 1)

    def test_capture_is_idempotent(self):
        msg = make_message(1000)
        self.assertTrue(capture_message(self.db, msg))
        self.assertFalse(capture_message(self.db, msg))
        self.assertEqual(self.db.stats()["messages"], 1)

    def test_author_name_is_snapshotted(self):
        """Display names change; the archive must keep what it was at capture time."""
        msg = make_message(1000, author=FakeAuthor(id=5, name="oldname",
                                                   display_name="OldName"))
        capture_message(self.db, msg)
        self.assertEqual(self.db.get_message(1000)["author_name"], "OldName")

    def test_bot_author_is_flagged(self):
        msg = make_message(1000, author=FakeAuthor(id=6, name="helper", bot=True))
        capture_message(self.db, msg)
        self.assertEqual(self.db.get_message(1000)["author_is_bot"], 1)

    def test_attachments_are_recorded_as_pending(self):
        msg = make_message(1000, attachments=[
            FakeAttachment(id="a1", filename="pic.png", url="https://cdn.example/a1")])
        capture_message(self.db, msg)
        atts = self.db.attachments_for(1000)
        self.assertEqual(len(atts), 1)
        self.assertEqual(atts[0]["download_status"], "pending")
        self.assertIsNone(atts[0]["local_path"])

    def test_attachments_recorded_even_on_duplicate_capture(self):
        """Self-healing: re-capturing must still register attachments."""
        plain = make_message(1000)
        capture_message(self.db, plain)
        self.assertEqual(len(self.db.attachments_for(1000)), 0)

        with_att = make_message(1000, attachments=[
            FakeAttachment(id="a1", filename="pic.png", url="https://cdn.example/a1")])
        self.assertFalse(capture_message(self.db, with_att))  # not new
        self.assertEqual(len(self.db.attachments_for(1000)), 1)  # but recorded

    def test_message_to_row_normalises_reply_and_embeds(self):
        from .stubs import FakeReference
        msg = make_message(1000, content="reply text")
        msg.reference = FakeReference(message_id=999)
        msg.embeds = [FakeEmbed(title="T", description="D")]
        row = message_to_row(msg)
        self.assertEqual(row["reply_to_message_id"], 999)
        self.assertEqual(row["embeds"], [{"title": "T", "description": "D"}])
        self.assertEqual(row["type"], 0)

    def test_timestamps_are_utc_iso(self):
        """message_to_row keeps datetimes; iso() normalises them at insert."""
        capture_message(self.db, make_message(1000))
        self.assertEqual(self.db.get_message(1000)["timestamp"],
                         "2026-08-27T12:00:00+00:00")

    def test_naive_datetime_is_treated_as_utc(self):
        msg = make_message(1000)
        msg.created_at = datetime(2026, 8, 27, 12, 0)  # no tzinfo
        capture_message(self.db, msg)
        self.assertEqual(self.db.get_message(1000)["timestamp"],
                         "2026-08-27T12:00:00+00:00")


class TestAttachmentPaths(unittest.TestCase):
    def test_safe_filename_strips_traversal_and_control_chars(self):
        self.assertEqual(safe_filename("../../etc/passwd"), "etc_passwd")
        self.assertEqual(safe_filename("hello world.png"), "hello_world.png")
        self.assertEqual(safe_filename(None), "attachment")
        self.assertEqual(safe_filename("..."), "attachment")

    def test_safe_filename_caps_length(self):
        self.assertLessEqual(len(safe_filename("a" * 500)), 180)

    def test_shard_path_is_content_addressed(self):
        digest = sha256_of(b"same bytes")
        p1 = shard_path("/data/att", digest, "one.png")
        p2 = shard_path("/data/att", digest, "two.png")
        self.assertEqual(p1.parent, p2.parent)  # same content, same directory
        self.assertTrue(str(p1).endswith("one.png"))
        self.assertIn(digest, str(p1))

    def test_identical_bytes_hash_identically(self):
        self.assertEqual(sha256_of(b"x"), sha256_of(b"x"))
        self.assertNotEqual(sha256_of(b"x"), sha256_of(b"y"))


class TestExport(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.db.upsert_guild(111, "Test Server", member_count=2)
        self.db.upsert_channel(222, 111, "general", type_=0, position=0)
        self.db.upsert_channel(223, 111, "media", type_=0, position=1)
        self.db.upsert_member(333, 111, "alice", display_name="Alice")
        self.db.upsert_member(444, 111, "bob", display_name="Bob")
        self.db.upsert_role(900, 111, "Admin", colour=0xFF0000, position=3)

        capture_message(self.db, make_message(
            1000, content="first message",
            author=FakeAuthor(id=333, name="alice", display_name="Alice")))
        capture_message(self.db, make_message(
            1001, content="has a <script> & \"quotes\"", minute=1,
            author=FakeAuthor(id=444, name="bob", display_name="Bob")))
        capture_message(self.db, make_message(
            1002, content="in media", minute=2,
            channel=FakeChannel(id=223, guild=FakeGuild(id=111), name="media"),
            attachments=[FakeAttachment(id="a1", filename="pic.png",
                                        url="https://cdn.example/a1")]))
        self.db.set_attachment_downloaded("a1", "/tmp/att/ab/abc/pic.png", "abc", 2048)
        self.db.record_edit(1000, "first message, edited")
        self.db.add_reaction(1000, "👍", 444)
        self.db.mark_deleted(1001)

    def tearDown(self):
        self.db.close()

    def test_json_contains_every_message_and_metadata(self):
        with TemporaryDirectory() as d:
            p = ex.export_json(self.db, Path(d) / "a.json", guild_id="111")
            data = json.loads(p.read_text(encoding="utf-8"))

        self.assertEqual(data["message_count"], 3)
        self.assertEqual(data["schema_version"], 1)
        by_id = {m["id"]: m for m in data["messages"]}
        self.assertEqual(by_id["1000"]["content"], "first message, edited")
        self.assertEqual(by_id["1000"]["edits"][0]["before"], "first message")
        self.assertEqual(by_id["1000"]["reactions"], [{"emoji": "👍", "user_id": "444"}])
        self.assertTrue(by_id["1001"]["deleted"])
        self.assertEqual(by_id["1002"]["attachments"][0]["local_path"],
                         "/tmp/att/ab/abc/pic.png")
        self.assertTrue(by_id["1002"]["attachments"][0]["downloaded"])

    def test_json_channel_filter(self):
        with TemporaryDirectory() as d:
            p = ex.export_json(self.db, Path(d) / "a.json", channel_id="223")
            data = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(data["message_count"], 1)
        self.assertEqual(data["messages"][0]["channel"], "media")

    def test_json_exclude_deleted(self):
        with TemporaryDirectory() as d:
            p = ex.export_json(self.db, Path(d) / "a.json", guild_id="111",
                               include_deleted=False)
            data = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(data["message_count"], 2)
        self.assertNotIn("1001", {m["id"] for m in data["messages"]})

    def test_csv_has_header_and_all_rows(self):
        with TemporaryDirectory() as d:
            p = ex.export_csv(self.db, Path(d) / "a.csv", guild_id="111")
            rows = list(csv.DictReader(p.read_text(encoding="utf-8").splitlines()))

        self.assertEqual(len(rows), 3)
        self.assertIn("content", rows[0])
        self.assertIn("attachments", rows[0])
        media = next(r for r in rows if r["channel"] == "media")
        self.assertIn("pic.png", media["attachments"])

    def test_html_escapes_untrusted_content(self):
        """Exported HTML must not execute injected markup from message text."""
        with TemporaryDirectory() as d:
            p = ex.export_html(self.db, Path(d) / "a.html", guild_id="111")
            html_out = p.read_text(encoding="utf-8")

        self.assertNotIn("<script>", html_out)
        self.assertIn("&lt;script&gt;", html_out)
        self.assertIn("Bob", html_out)
        self.assertIn("deleted", html_out)   # deletion badge
        self.assertIn("edited", html_out)    # edit badge
        self.assertIn("3 messages", html_out)

    def test_html_renders_local_image_when_downloaded(self):
        with TemporaryDirectory() as d:
            p = ex.export_html(self.db, Path(d) / "a.html", channel_id="223")
            html_out = p.read_text(encoding="utf-8")
        self.assertIn("<img", html_out)
        self.assertIn("pic.png", html_out)

    def test_export_all_writes_three_files(self):
        with TemporaryDirectory() as d:
            paths = ex.export_all(self.db, Path(d) / "out", guild_id="111")
            self.assertEqual(set(paths), {"json", "csv", "html"})
            for p in paths.values():
                self.assertTrue(p.exists())
                self.assertGreater(p.stat().st_size, 0)


class TestServerInfo(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.db.upsert_guild(111, "Test Server", member_count=2,
                             meta={"owner_id": "333"})
        self.db.upsert_channel(222, 111, "general", type_=0, position=0,
                               topic="Welcome")
        self.db.upsert_channel(223, 111, "media", type_=0, position=1)
        self.db.upsert_member(333, 111, "alice", display_name="Alice", is_bot=False)
        self.db.upsert_member(444, 111, "helper", display_name="Helper", is_bot=True)
        self.db.upsert_role(900, 111, "Admin", colour=0xFF0000, position=3)
        ch = FakeChannel(id=222, guild=FakeGuild(id=111, name="Test Server"),
                         name="general", topic="Welcome")
        capture_message(self.db, make_message(1000, content="hi", channel=ch))

    def tearDown(self):
        self.db.close()

    def test_server_info_payload_shape(self):
        info = ex.server_info(self.db, "111")
        self.assertEqual(info["guild"]["name"], "Test Server")
        self.assertEqual(info["guild"]["member_count"], 2)
        self.assertEqual(len(info["channels"]), 2)
        self.assertEqual(len(info["members"]), 2)
        self.assertEqual(len(info["roles"]), 1)
        self.assertEqual(info["totals"]["messages"], 1)
        gen = next(c for c in info["channels"] if c["name"] == "general")
        self.assertEqual(gen["messages"], 1)
        self.assertEqual(gen["topic"], "Welcome")

    def test_server_info_unknown_guild_raises(self):
        with self.assertRaises(KeyError):
            ex.server_info(self.db, "999")

    def test_export_server_info_writes_three_files(self):
        with TemporaryDirectory() as d:
            paths = ex.export_server_info(self.db, "111", Path(d) / "info")
            self.assertEqual(set(paths), {"json", "members_csv", "channels_csv"})
            data = json.loads(paths["json"].read_text(encoding="utf-8"))
            self.assertEqual(data["guild"]["id"], "111")
            members = list(csv.DictReader(
                paths["members_csv"].read_text(encoding="utf-8").splitlines()))
            self.assertEqual(len(members), 2)
            self.assertIn("is_bot", members[0])


class TestSchemaDrivesFormats(unittest.TestCase):
    def test_csv_and_json_field_lists_are_consistent(self):
        csv_names = [f.name for f in ex.CSV_FIELDS]
        json_names = [f.name for f in ex.JSON_FIELDS]
        for n in csv_names:
            self.assertIn(n, json_names, f"{n} in CSV but missing from JSON")

    def test_every_schema_field_maps_to_a_real_key(self):
        """Guards against a Field.source typo silently emitting nulls."""
        db = Database(":memory:")
        db.upsert_guild(111, "T")
        db.upsert_channel(222, 111, "general")
        capture_message(db, make_message(1, content="x"))
        row = db.get_message(1)
        flat = ex.flatten_message(db, row)
        for f in ex.EXPORT_SCHEMA:
            self.assertIn(f.source, flat, f"{f.name} -> {f.source} not in flattened dict")
        db.close()


if __name__ == "__main__":
    unittest.main()


class TestCaptureColdStart(unittest.TestCase):
    """Regression: capture against a database with nothing pre-created.

    The original bug was that capture_message created channel and author rows
    but never the guild row, so the very first live message raised
    sqlite3.IntegrityError: FOREIGN KEY constraint failed. The rest of this
    file could not catch it, because setUp() pre-created the guild.
    """

    def setUp(self):
        # Deliberately empty. No upsert_guild, no upsert_channel.
        self.db = Database(":memory:")

    def tearDown(self):
        self.db.close()

    def test_capture_succeeds_on_a_completely_empty_database(self):
        """The exact production failure: first message, nothing catalogued."""
        self.assertTrue(capture_message(self.db, make_message(1000)))
        self.assertEqual(self.db.stats()["messages"], 1)

    def test_guild_row_is_created_from_the_message(self):
        capture_message(self.db, make_message(1000))
        g = self.db.conn.execute("SELECT * FROM guilds").fetchone()
        self.assertIsNotNone(g, "guild row must be created on demand")
        self.assertEqual(g["id"], "111")
        self.assertEqual(g["name"], "Test Server")

    def test_channel_and_member_are_parented_to_that_guild(self):
        capture_message(self.db, make_message(1000))
        self.assertEqual(
            self.db.conn.execute("SELECT guild_id FROM channels").fetchone()["guild_id"], "111")
        self.assertEqual(
            self.db.conn.execute("SELECT guild_id FROM members").fetchone()["guild_id"], "111")

    def test_many_messages_do_not_re_upsert_the_guild(self):
        """The cache must hold, otherwise a busy server costs a write per message."""
        capture_message(self.db, make_message(1000))
        self.db.conn.execute("UPDATE guilds SET name = 'renamed by hand'")
        self.db.conn.commit()

        capture_message(self.db, make_message(1001))
        name = self.db.conn.execute("SELECT name FROM guilds").fetchone()["name"]
        self.assertEqual(name, "renamed by hand",
                         "second message must not overwrite the guild row")

    def test_dm_message_is_skipped_rather_than_crashing(self):
        """A DM has no guild, and every table hangs off one, so skip it cleanly."""
        from .stubs import FakeChannel
        ch = FakeChannel(id=999, guild=None, name="dm")
        msg = make_message(2000, channel=ch)
        self.assertFalse(capture_message(self.db, msg))
        self.assertEqual(self.db.stats()["messages"], 0)

    def test_live_capture_does_not_wipe_catalogued_metadata(self):
        """backfill.catalog_guild() writes owner_id/created_at and a member
        count. A live message arriving afterwards carries a partial view of the
        guild and must not blank any of that out."""
        self.db.upsert_guild(111, "Test Server", member_count=2,
                             meta={"owner_id": "333", "created_at": "2025-12-31"})

        capture_message(self.db, make_message(1000))

        g = self.db.conn.execute("SELECT * FROM guilds WHERE id='111'").fetchone()
        self.assertEqual(g["member_count"], 2, "member_count was blanked")
        self.assertIsNotNone(g["meta_json"], "meta_json was blanked")
        self.assertIn("owner_id", g["meta_json"])
        self.assertIn("created_at", g["meta_json"])


class TestCaptureColdStartWithRoles(unittest.TestCase):
    """Second regression: the author has roles.

    TestCaptureColdStart used the stub default of roles=[], so it never
    exercised member_roles.role_id -> roles(id). Roles were only created by
    catalog_guild, so a live message from a member with any role at all raised
    IntegrityError. Every foreign key in the schema must be satisfiable from
    the live capture path alone.
    """

    def setUp(self):
        self.db = Database(":memory:")  # nothing pre-created

    def tearDown(self):
        self.db.close()

    def _authored_message(self, mid):
        from .stubs import FakeRole
        author = FakeAuthor(
            id=333, name="alice", display_name="Alice",
            roles=[FakeRole(id=111, name="@everyone"),
                   FakeRole(id=900, name="Admin"),
                   FakeRole(id=901, name="Members")],
        )
        return make_message(mid, author=author)

    def test_roles_are_created_on_demand(self):
        self.assertTrue(capture_message(self.db, self._authored_message(1000)))
        roles = self.db.conn.execute("SELECT id, name FROM roles ORDER BY id").fetchall()
        self.assertEqual([r["id"] for r in roles], ["111", "900", "901"])
        self.assertEqual([r["name"] for r in roles], ["@everyone", "Admin", "Members"])

    def test_member_role_links_are_written(self):
        capture_message(self.db, self._authored_message(1000))
        links = self.db.conn.execute(
            "SELECT role_id FROM member_roles WHERE member_id='333' ORDER BY role_id").fetchall()
        self.assertEqual([l["role_id"] for l in links], ["111", "900", "901"])

    def test_roles_are_not_re_inserted_per_message(self):
        capture_message(self.db, self._authored_message(1000))
        self.db.conn.execute("UPDATE roles SET name = 'renamed' WHERE id='900'")
        self.db.conn.commit()

        capture_message(self.db, self._authored_message(1001))
        name = self.db.conn.execute(
            "SELECT name FROM roles WHERE id='900'").fetchone()["name"]
        self.assertEqual(name, "renamed", "cache failed; role row was overwritten")

    def test_fully_populated_message_on_empty_database(self):
        """Every foreign key satisfied from the live path, nothing pre-created."""
        from .stubs import FakeAttachment, FakeEmbed, FakeReference, FakeRole
        author = FakeAuthor(id=333, name="alice", display_name="Alice",
                            roles=[FakeRole(id=900, name="Admin")])
        msg = make_message(1000, author=author, content="everything at once",
                           attachments=[FakeAttachment(id="a1", filename="x.png",
                                                       url="https://cdn/x.png")])
        msg.reference = FakeReference(message_id=999)
        msg.embeds = [FakeEmbed(title="T")]

        self.assertTrue(capture_message(self.db, msg))
        self.assertTrue(self.db.add_reaction(1000, "👍", 444))
        self.assertTrue(self.db.record_edit(1000, "edited"))
        self.db.mark_deleted(1000)

        # Integrity check across every child table.
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) n FROM guilds").fetchone()["n"], 1)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) n FROM roles").fetchone()["n"], 1)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) n FROM member_roles").fetchone()["n"], 1)
        self.assertEqual(len(self.db.attachments_for(1000)), 1)
        self.assertEqual(len(self.db.edits_for(1000)), 1)
        self.assertEqual(len(self.db.reactions_for(1000)), 1)
        self.assertEqual(self.db.get_message(1000)["deleted"], 1)

    def test_reaction_on_unarchived_message_is_ignored_not_fatal(self):
        """on_raw_reaction_add fires for messages the backfill has not reached."""
        self.assertFalse(self.db.add_reaction(999999, "👍", 444))
        self.assertEqual(
            self.db.conn.execute("SELECT COUNT(*) n FROM reactions").fetchone()["n"], 0)


class TestForeignKeyCoverage(unittest.TestCase):
    """Guard against the next one: assert the live path satisfies every FK."""

    def test_every_fk_parent_is_reachable_from_capture(self):
        import re
        import sqlite3
        from archiver.db import SCHEMA

        con = sqlite3.connect(":memory:")
        con.executescript(SCHEMA)
        parents = set()
        for (_tbl, sql) in con.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table'"):
            for m in re.finditer(r"REFERENCES\s+(\w+)\(", sql or ""):
                parents.add(m.group(1))

        db = Database(":memory:")
        try:
            from .stubs import FakeRole
            author = FakeAuthor(id=333, name="alice", display_name="Alice",
                                roles=[FakeRole(id=900, name="Admin")])
            capture_message(db, make_message(1000, author=author))

            # After a single cold-start capture, no parent table referenced by a
            # foreign key may still be empty.
            for parent in sorted(parents):
                if parent in {"messages", "reactions", "message_edits",
                              "attachments", "sync_state", "member_roles"}:
                    continue  # children, not parents the live path must seed
                n = db.conn.execute(f"SELECT COUNT(*) n FROM {parent}").fetchone()["n"]
                self.assertGreater(n, 0, f"{parent} was left empty after capture")
        finally:
            db.close()
            con.close()
