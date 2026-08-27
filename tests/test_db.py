"""Tests for the storage layer."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from archiver.db import Database


class TestSchema(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")

    def tearDown(self):
        self.db.close()

    def test_all_tables_created(self):
        rows = self.db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        names = {r["name"] for r in rows}
        for expected in {"guilds", "channels", "roles", "members", "member_roles",
                         "messages", "attachments", "message_edits", "message_deletes",
                         "reactions", "sync_state", "meta"}:
            self.assertIn(expected, names)

    def test_schema_is_idempotent(self):
        self.db.init_schema()
        self.db.init_schema()
        # Parent rows first: the FK constraint is ON, so an orphan insert must fail.
        self.db.upsert_guild(111, "Test Server")
        self.db.upsert_channel(222, 111, "general")
        self.db.insert_message(self._msg(1))
        self.assertEqual(self.db.stats()["messages"], 1)

    def test_foreign_keys_reject_orphan_messages(self):
        with self.assertRaises(Exception):
            self.db.insert_message(self._msg(1))  # no guild 111 / channel 222

    def _msg(self, mid, **kw):
        base = dict(id=mid, channel_id=222, guild_id=111, author_id=333,
                    author_name="Alice", content="hello",
                    timestamp=datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc))
        base.update(kw)
        return base


class TestMessages(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.db.upsert_guild(111, "Test Server", member_count=2)
        self.db.upsert_channel(222, 111, "general", type_=0, position=0)

    def tearDown(self):
        self.db.close()

    def _msg(self, mid, **kw):
        base = dict(id=mid, channel_id=222, guild_id=111, author_id=333,
                    author_name="Alice", content=f"message {mid}",
                    timestamp=datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc))
        base.update(kw)
        return base

    def test_insert_returns_true_then_false_for_duplicate(self):
        """Dedup is the core correctness property: history and live overlap."""
        self.assertTrue(self.db.insert_message(self._msg(1)))
        self.assertFalse(self.db.insert_message(self._msg(1)))
        self.assertEqual(self.db.stats()["messages"], 1)

    def test_duplicate_insert_does_not_overwrite(self):
        self.db.insert_message(self._msg(1, content="original"))
        self.db.insert_message(self._msg(1, content="clobbered"))
        self.assertEqual(self.db.get_message(1)["content"], "original")

    def test_ids_are_stored_as_text(self):
        big = "1234567890123456789"  # 19 digits, beyond JS MAX_SAFE_INTEGER
        self.db.insert_message(self._msg(big))
        self.assertEqual(self.db.get_message(big)["id"], big)

    def test_mark_deleted_sets_flag_and_logs(self):
        self.db.insert_message(self._msg(1))
        self.db.mark_deleted(1)
        row = self.db.get_message(1)
        self.assertEqual(row["deleted"], 1)
        self.assertIsNotNone(row["deleted_at"])
        self.assertEqual(
            self.db.conn.execute("SELECT COUNT(*) n FROM message_deletes").fetchone()["n"], 1)
        # Content survives deletion - that is the whole point of archiving.
        self.assertEqual(row["content"], "message 1")

    def test_record_edit_changes_content_and_logs_history(self):
        self.db.insert_message(self._msg(1, content="before"))
        self.assertTrue(self.db.record_edit(1, "after"))
        self.assertEqual(self.db.get_message(1)["content"], "after")
        edits = self.db.edits_for(1)
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["before_content"], "before")
        self.assertEqual(edits[0]["after_content"], "after")

    def test_record_edit_is_noop_when_unchanged(self):
        self.db.insert_message(self._msg(1, content="same"))
        self.assertFalse(self.db.record_edit(1, "same"))
        self.assertEqual(len(self.db.edits_for(1)), 0)

    def test_record_edit_unknown_message_returns_false(self):
        self.assertFalse(self.db.record_edit(999, "x"))

    def test_reaction_is_unique_per_user_and_emoji(self):
        self.db.insert_message(self._msg(1))
        self.db.add_reaction(1, "👍", 333)
        self.db.add_reaction(1, "👍", 333)  # duplicate
        self.db.add_reaction(1, "🎉", 333)
        self.assertEqual(len(self.db.reactions_for(1)), 2)

    def test_iter_messages_is_chronological_by_snowflake(self):
        for i in (300, 100, 200):
            self.db.insert_message(self._msg(i))
        ids = [r["id"] for r in self.db.iter_messages(channel_id=222)]
        self.assertEqual(ids, ["100", "200", "300"])

    def test_iter_messages_can_exclude_deleted(self):
        self.db.insert_message(self._msg(1))
        self.db.insert_message(self._msg(2))
        self.db.mark_deleted(2)
        kept = list(self.db.iter_messages(include_deleted=False))
        self.assertEqual([r["id"] for r in kept], ["1"])
        self.assertEqual(len(list(self.db.iter_messages(include_deleted=True))), 2)


class TestSyncState(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.db.upsert_guild(111, "Test Server")
        self.db.upsert_channel(222, 111, "general", type_=0, position=0)
        self.db.upsert_channel(223, 111, "voice", type_=2, position=1)

    def tearDown(self):
        self.db.close()

    def test_cursor_accumulates_count_across_batches(self):
        self.db.set_backfill_cursor(222, "500", increment=100)
        self.db.set_backfill_cursor(222, "400", increment=50)
        s = self.db.get_sync(222)
        self.assertEqual(s["oldest_message_id"], "400")
        self.assertEqual(s["backfill_message_count"], 150)
        self.assertEqual(s["backfill_complete"], 0)

    def test_voice_channels_are_excluded_from_backfill(self):
        pending = self.db.channels_needing_backfill(111)
        self.assertEqual([c["id"] for c in pending], ["222"])

    def test_completed_channel_drops_off_the_pending_list(self):
        self.assertEqual(len(self.db.channels_needing_backfill(111)), 1)
        self.db.mark_backfill_complete(222)
        self.assertEqual(len(self.db.channels_needing_backfill(111)), 0)

    def test_mark_complete_creates_row_if_absent(self):
        self.db.upsert_channel(224, 111, "announcements", type_=5, position=2)
        self.db.mark_backfill_complete(224)
        self.assertEqual(self.db.get_sync(224)["backfill_complete"], 1)


class TestAttachments(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.db.upsert_guild(111, "Test Server")
        self.db.upsert_channel(222, 111, "general")
        self.db.insert_message(dict(
            id=1, channel_id=222, guild_id=111, author_id=333, author_name="Alice",
            content="look", timestamp=datetime(2026, 8, 27, tzinfo=timezone.utc)))

    def tearDown(self):
        self.db.close()

    def _att(self, aid, **kw):
        base = dict(id=aid, message_id=1, channel_id=222, filename="a.png",
                    url=f"https://cdn.example/{aid}", size=100,
                    content_type="image/png", download_status="pending")
        base.update(kw)
        return base

    def test_add_is_idempotent(self):
        self.assertTrue(self.db.add_attachment(self._att("x1")))
        self.assertFalse(self.db.add_attachment(self._att("x1")))

    def test_pending_query_and_status_transitions(self):
        self.db.add_attachment(self._att("x1"))
        self.db.add_attachment(self._att("x2"))
        self.assertEqual(len(self.db.pending_attachments()), 2)

        self.db.set_attachment_downloaded("x1", "/tmp/x1.png", "abc123", 100)
        self.assertEqual(len(self.db.pending_attachments()), 1)

        self.db.set_attachment_failed("x2")
        self.assertEqual(len(self.db.pending_attachments()), 0)
        row = self.db.conn.execute(
            "SELECT download_status FROM attachments WHERE id='x2'").fetchone()
        self.assertEqual(row["download_status"], "failed")

    def test_sha_lookup_finds_downloaded_duplicates(self):
        self.db.add_attachment(self._att("x1"))
        self.db.set_attachment_downloaded("x1", "/tmp/x1.png", "deadbeef", 100)
        found = self.db.find_attachment_by_sha("deadbeef")
        self.assertIsNotNone(found)
        self.assertEqual(found["local_path"], "/tmp/x1.png")
        self.assertIsNone(self.db.find_attachment_by_sha("nope"))


class TestStats(unittest.TestCase):
    def test_empty_database_reports_zeros_not_none(self):
        db = Database(":memory:")
        s = db.stats()
        self.assertEqual(s["messages"], 0)
        self.assertEqual(s["deleted_messages"], 0)
        self.assertEqual(s["attachments"], 0)
        self.assertEqual(s["attachment_bytes"], 0)
        db.close()


if __name__ == "__main__":
    unittest.main()
