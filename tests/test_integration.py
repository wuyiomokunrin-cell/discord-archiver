"""End-to-end integration test: populate -> export -> serve.

Exercises the whole offline pipeline the way the CLI does, using the Flask test
client so no socket is opened. This is what catches wiring bugs that unit tests
on individual modules cannot see.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from archiver import export as ex
from archiver.capture import capture_message
from archiver.db import Database
from dashboard.app import create_app

from .stubs import (FakeAttachment, FakeAuthor, FakeChannel, FakeGuild,
                    make_message)

GUILD_NAME = "Integration Server"


class TestFullPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

        self.guild = FakeGuild(id=111, name=GUILD_NAME)
        self.general = FakeChannel(id=222, guild=self.guild, name="general",
                                   topic="Talk about anything")
        self.media = FakeChannel(id=223, guild=self.guild, name="media")

        self.db = Database(self.root / "archive.sqlite3")
        self.addCleanup(self.db.close)

        # Catalogue first, the way backfill.catalog_guild does, then let live
        # capture arrive afterwards. That ordering is the one that regressed.
        self.db.upsert_guild(111, GUILD_NAME, member_count=3,
                             meta={"owner_id": "333"})
        self.db.upsert_channel(222, 111, "general", type_=0, position=0,
                               topic="Talk about anything")
        self.db.upsert_channel(223, 111, "media", type_=0, position=1)
        self.db.upsert_role(900, 111, "Admin", colour=0xFF0000, position=3)

        alice = FakeAuthor(id=333, name="alice", display_name="Alice")
        bob = FakeAuthor(id=444, name="bob", display_name="Bob")

        capture_message(self.db, make_message(
            1000, content="hello world", channel=self.general, author=alice))
        capture_message(self.db, make_message(
            1001, content="a picture", channel=self.media, author=bob, minute=1,
            attachments=[FakeAttachment(id="a1", filename="pic.png",
                                        url="https://cdn.example/a1")]))
        capture_message(self.db, make_message(
            1002, content="<b>markup</b> & more", channel=self.general,
            author=bob, minute=2))

        self.db.set_attachment_downloaded(
            "a1", str(self.root / "attachments" / "pic.png"), "abc123", 2048)
        self.db.upsert_member(333, 111, "alice", display_name="Alice")
        self.db.upsert_member(444, 111, "bob", display_name="Bob")
        self.db.upsert_member(555, 111, "helper", display_name="Helper", is_bot=True)

    # ------------------------------------------------------------- database

    def test_catalogue_then_live_capture_preserves_metadata(self):
        g = self.db.conn.execute("SELECT * FROM guilds WHERE id='111'").fetchone()
        self.assertEqual(g["name"], GUILD_NAME)
        self.assertEqual(g["member_count"], 3)
        self.assertIn("owner_id", g["meta_json"])

    def test_stats_reflect_what_was_captured(self):
        s = self.db.stats("111")
        self.assertEqual(s["messages"], 3)
        self.assertEqual(s["attachments"], 1)
        self.assertEqual(s["attachments_downloaded"], 1)
        self.assertEqual(s["deleted_messages"], 0)

    # --------------------------------------------------------------- export

    def test_export_round_trips(self):
        paths = ex.export_all(self.db, self.root / "exports", guild_id="111",
                              title=GUILD_NAME)
        self.assertEqual(set(paths), {"json", "csv", "html"})

        data = json.loads(paths["json"].read_text(encoding="utf-8"))
        self.assertEqual(data["message_count"], 3)
        by_id = {m["id"]: m for m in data["messages"]}
        self.assertEqual(by_id["1000"]["channel"], "general")
        self.assertEqual(by_id["1001"]["attachments"][0]["downloaded"], True)

        html_out = paths["html"].read_text(encoding="utf-8")
        self.assertNotIn("<b>markup</b>", html_out, "message HTML was not escaped")
        self.assertIn("&lt;b&gt;markup&lt;/b&gt;", html_out)
        self.assertIn("Alice", html_out)

    def test_server_info_lists_every_channel_with_counts(self):
        info = ex.server_info(self.db, "111")
        self.assertEqual(info["guild"]["name"], GUILD_NAME)
        self.assertEqual(len(info["channels"]), 2)
        self.assertEqual(len(info["members"]), 3)
        self.assertEqual(len(info["roles"]), 1)
        counts = {c["name"]: c["messages"] for c in info["channels"]}
        self.assertEqual(counts, {"general": 2, "media": 1})

    # ------------------------------------------------------------ dashboard

    def test_dashboard_serves_every_route(self):
        app = create_app(self.root / "archive.sqlite3")
        c = app.test_client()
        for route in ["/", "/api/stats", "/api/guilds", "/api/channels",
                      "/api/members", "/api/channel/222", "/export/json",
                      "/export/csv"]:
            r = c.get(route)
            self.assertEqual(r.status_code, 200, f"{route} -> {r.status_code}")

    def test_dashboard_search_finds_and_filters(self):
        c = create_app(self.root / "archive.sqlite3").test_client()

        hits = c.get("/api/search?q=hello").get_json()
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["content"], "hello world")

        scoped = c.get("/api/search?q=a&channel=223").get_json()
        self.assertTrue(all(h["channel_id"] == "223" for h in scoped))

        by_author = c.get("/api/search?author=Bob").get_json()
        self.assertTrue(by_author)
        self.assertTrue(all(h["author_name"] == "Bob" for h in by_author))

    def test_dashboard_index_renders_real_data(self):
        html_out = create_app(self.root / "archive.sqlite3").test_client() \
            .get("/").get_data(as_text=True)
        self.assertIn(GUILD_NAME, html_out)
        self.assertIn("general", html_out)
        self.assertIn("media", html_out)

    def test_dashboard_export_endpoint_returns_the_archive(self):
        c = create_app(self.root / "archive.sqlite3").test_client()
        r = c.get("/export/json")
        self.assertEqual(r.status_code, 200)
        payload = json.loads(r.get_data(as_text=True))
        self.assertEqual(payload["message_count"], 3)

    def test_dashboard_unknown_export_format_404s(self):
        c = create_app(self.root / "archive.sqlite3").test_client()
        self.assertEqual(c.get("/export/pdf").status_code, 404)


if __name__ == "__main__":
    unittest.main()
