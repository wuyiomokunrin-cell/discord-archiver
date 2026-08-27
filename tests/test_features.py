"""Tests for the added features: ping, audit capture, portable lock, dashboard."""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import discord

from archiver import audit, lock
from archiver.db import Database
from archiver.listener import ArchiverClient

from .stubs import FakeGuild


@dataclass
class FakeUser:
    id: int = 1

    def mentioned_in(self, message) -> bool:
        return f"@{self.id}" in (message.content or "")


def make_client(db, guild_id=111):
    c = ArchiverClient.__new__(ArchiverClient)
    c.db = db
    c.target_guild_id = guild_id
    c._connection = type("Conn", (), {"user": FakeUser(id=1)})()  # Client.user reads this
    c.counters = {"messages": 0, "edits": 0, "deletes": 0, "reactions": 0,
                  "skipped": 0, "pings": 0, "audit": 0}
    c.mirror = None
    return c


@dataclass
class FakeMsg:
    content: str = ""


class TestPing(unittest.TestCase):
    def setUp(self):
        self.c = make_client(Database(":memory:"))

    def test_bang_ping(self):
        self.assertTrue(self.c._is_ping(FakeMsg("!ping")))
        self.assertTrue(self.c._is_ping(FakeMsg("!PING now")))

    def test_mention_ping(self):
        self.assertTrue(self.c._is_ping(FakeMsg("hey @1 ping")))

    def test_normal_message_is_not_a_ping(self):
        self.assertFalse(self.c._is_ping(FakeMsg("pings are tasty")))
        self.assertFalse(self.c._is_ping(FakeMsg("@1 hello")))  # mention without ping
        self.assertFalse(self.c._is_ping(FakeMsg("")))


class TestDiff(unittest.TestCase):
    def test_detects_change(self):
        b = type("B", (), {"name": "old", "topic": "same"})()
        a = type("A", (), {"name": "new", "topic": "same"})()
        self.assertEqual(audit.diff(b, a, ["name", "topic"]),
                         {"name": ["old", "new"]})

    def test_no_change_empty(self):
        b = type("B", (), {"name": "x"})()
        a = type("A", (), {"name": "x"})()
        self.assertEqual(audit.diff(b, a, ["name"]), {})

    def test_value_objects_and_role_lists_normalised(self):
        b = type("B", (), {"colour": type("C", (), {"value": 1})(),
                           "roles": [type("R", (), {"id": 7})()]})()
        a = type("A", (), {"colour": type("C", (), {"value": 2})(),
                           "roles": [type("R", (), {"id": 7})(), type("R", (), {"id": 8})()]})()
        d = audit.diff(b, a, ["colour", "roles"])
        self.assertEqual(d["colour"], [1, 2])
        self.assertEqual(d["roles"], [[7], [7, 8]])


class TestAuditStorage(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.db.upsert_guild(111, "T")

    def tearDown(self):
        self.db.close()

    def test_record_live_and_query(self):
        audit.record_live(self.db, 111, "role.create", "role", 900, "Admin")
        rows = self.db.audit_events("111")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "role.create")
        self.assertEqual(rows[0]["target_name"], "Admin")

    def test_guild_filter(self):
        audit.record_live(self.db, 111, "a", "role", 1, "x")
        audit.record_live(self.db, 999, "b", "role", 2, "y")
        self.assertEqual(len(self.db.audit_events("111")), 1)
        self.assertEqual(len(self.db.audit_events(None)), 2)


@dataclass
class FakeAuditEntry:
    id: int
    guild: object
    action: object
    user: object = None
    target: object = None
    reason: str | None = None
    created_at: datetime = field(
        default_factory=lambda: datetime(2026, 8, 27, tzinfo=timezone.utc))


@dataclass
class FakeAuditAction:
    name: str = "channel_delete"


@dataclass
class FakeAuditGuild:
    id: int = 111
    entries: list = field(default_factory=list)

    def audit_logs(self, limit=None, oldest_first=False, **kw):
        entries = list(self.entries)
        if oldest_first:
            entries.reverse() if False else None
        outer = entries

        class _Iter:
            def __init__(self):
                self.items = list(outer)
                self.i = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.i >= len(self.items):
                    raise StopAsyncIteration
                e = self.items[self.i]
                self.i += 1
                return e
        return _Iter()


class TestPullAuditLogs(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.db.upsert_guild(111, "T")

    def tearDown(self):
        self.db.close()

    def test_records_and_cursors(self):
        g = FakeAuditGuild(id=111, entries=[
            FakeAuditEntry(id=10, guild=g0, action=FakeAuditAction("channel_delete"),
                           target=type("T", (), {"id": 5, "name": "old"})())
            for g0 in [FakeGuild(id=111)]
        ])
        n = asyncio.run(audit.pull_audit_logs(self.db, g))
        self.assertEqual(n, 1)
        self.assertEqual(self.db.audit_events("111")[0]["event"], "channel_delete")
        self.assertEqual(self.db.get_meta("audit_cursor:111"), "10")

        # second run with same entries: cursor prevents re-recording
        n2 = asyncio.run(audit.pull_audit_logs(self.db, g))
        self.assertEqual(n2, 0)

    def test_new_entries_after_cursor_are_recorded(self):
        g = FakeAuditGuild(id=111, entries=[FakeAuditEntry(
            id=10, guild=FakeGuild(id=111), action=FakeAuditAction())])
        asyncio.run(audit.pull_audit_logs(self.db, g))
        g.entries.append(FakeAuditEntry(id=11, guild=FakeGuild(id=111),
                                        action=FakeAuditAction("member_ban")))
        n = asyncio.run(audit.pull_audit_logs(self.db, g))
        self.assertEqual(n, 1)


class TestAuditHandlers(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.db.upsert_guild(111, "T")
        self.c = make_client(self.db)

    def tearDown(self):
        self.db.close()

    def test_role_update_records_diff(self):
        g = FakeGuild(id=111)
        before = type("B", (), {"guild": g, "id": 900, "name": "Old",
                                "colour": type("C", (), {"value": 1})(),
                                "position": 1})()
        after = type("A", (), {"guild": g, "id": 900, "name": "New",
                               "colour": type("C", (), {"value": 2})(),
                               "position": 1})()
        asyncio.run(self.c.on_guild_role_update(before, after))
        rows = self.db.audit_events("111")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "role.update")
        self.assertEqual(rows[0]["target_name"], "New")
        self.assertIn("name", rows[0]["after_json"])

    def test_member_ban_records(self):
        g = FakeGuild(id=111)
        user = type("U", (), {"id": 77, "name": "bad"})()
        asyncio.run(self.c.on_member_ban(g, user))
        self.assertEqual(self.db.audit_events("111")[0]["event"], "member.ban")

    def test_out_of_scope_guild_ignored(self):
        other = FakeGuild(id=999)
        user = type("U", (), {"id": 77, "name": "bad"})()
        asyncio.run(self.c.on_member_ban(other, user))
        self.assertEqual(len(self.db.audit_events(None)), 0)


class TestLockPortable(unittest.TestCase):
    def test_acquire_and_refuse(self):
        with TemporaryDirectory() as d:
            db = Path(d) / "x.sqlite3"
            first = lock.acquire(db)
            try:
                with self.assertRaises(SystemExit):
                    lock.acquire(db)
            finally:
                first.close()
            second = lock.acquire(db)  # reusable after release
            second.close()


class TestDashboardNewEndpoints(unittest.TestCase):
    def test_roles_and_audit_endpoints(self):
        from dashboard.app import create_app
        with TemporaryDirectory() as d:
            db = Database(Path(d) / "a.sqlite3")
            db.upsert_guild(111, "T")
            db.upsert_role(900, 111, "Admin", colour=0xFF, position=3)
            audit.record_live(db, 111, "channel.create", "channel", 5, "gen")
            db.close()

            c = create_app(Path(d) / "a.sqlite3").test_client()
            roles = c.get("/api/roles")
            self.assertEqual(roles.status_code, 200)
            self.assertEqual(roles.get_json()[0]["name"], "Admin")

            ev = c.get("/api/audit")
            self.assertEqual(ev.status_code, 200)
            self.assertEqual(ev.get_json()[0]["event"], "channel.create")

    def test_message_attachments_and_file_route(self):
        import base64
        from dashboard.app import create_app
        with TemporaryDirectory() as d:
            db = Database(Path(d) / "a.sqlite3")
            db.upsert_guild(111, "T")
            db.upsert_channel(1, 111, "gen", "text", 0)
            db.insert_message({"id": 1, "guild_id": 111, "channel_id": 1,
                               "author_id": 2, "author_name": "a", "content": "pic",
                               "timestamp": "2026-01-01T00:00:00+00:00", "type": 0})
            db.add_attachment({"id": 77, "message_id": 1, "channel_id": 1,
                               "filename": "pic.png", "url": "https://x/y.png",
                               "content_type": "image/png", "width": 1, "height": 1})
            png = base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
                "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
            img = Path(d) / "pic.png"; img.write_bytes(png)
            db.set_attachment_downloaded(77, str(img), "beef", len(png))
            db.close()

            c = create_app(Path(d) / "a.sqlite3").test_client()
            msgs = c.get("/api/channel/1").get_json()
            self.assertEqual(len(msgs[0]["attachments"]), 1)
            self.assertTrue(msgs[0]["attachments"][0]["local_path"])

            searched = c.get("/api/search?q=pic").get_json()
            self.assertEqual(len(searched[0]["attachments"]), 1)

            f = c.get("/file/77")
            self.assertEqual(f.status_code, 200)
            self.assertEqual(f.mimetype, "image/png")
            self.assertEqual(f.data, png)
            f.close()  # send_file wraps an open file; release it like a server would

            # an attachment with no local file 404s
            self.assertEqual(c.get("/file/999").status_code, 404)


if __name__ == "__main__":
    unittest.main()
