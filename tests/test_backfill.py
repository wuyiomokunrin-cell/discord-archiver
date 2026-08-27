"""Tests for backfill.catalog_guild and backfill_channel.

These had no coverage at all, because they need a live discord.Guild. That is
why `role.is_bot()` - a method that does not exist on discord.Role - shipped:
nothing ever called catalog_guild. The fakes below mirror the real API surface
so the code path is exercised offline.
"""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timezone

import discord

from archiver import backfill
from archiver.capture import channel_category_id, role_is_bot
from archiver.db import Database

from .stubs import FakeGuild, make_message


@dataclass
class FakeColour:
    value: int = 0x3498DB


@dataclass
class FakeRole:
    id: int
    name: str = "role"
    colour: FakeColour = field(default_factory=FakeColour)
    position: int = 0
    _bot_managed: bool = False

    def is_bot_managed(self) -> bool:
        return self._bot_managed


@dataclass
class FakeCategory:
    id: int


@dataclass
class FakeAvatar:
    url: str = "https://cdn.example/a.png"


@dataclass
class FakeMember:
    id: int
    name: str = "someone"
    display_name: str = "Someone"
    bot: bool = False
    joined_at: datetime | None = None
    display_avatar: FakeAvatar | None = field(default_factory=FakeAvatar)
    roles: list = field(default_factory=list)


@dataclass
class FakeHistoryChannel:
    """Text channel whose history() is an async iterator, newest first."""
    id: int
    guild: object
    name: str = "general"
    type: object = field(default_factory=lambda: discord.ChannelType.text)
    position: int = 0
    category: object | None = None
    topic: str | None = None
    nsfw: bool = False
    messages: list = field(default_factory=list)
    forbidden: bool = False

    def history(self, limit=None, before=None, oldest_first=False):
        outer = self

        class _Iter:
            def __init__(self):
                self.items = list(outer.messages)
                if before is not None:
                    self.items = [m for m in self.items if m.id < before.id]
                self.i = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.i >= len(self.items):
                    raise StopAsyncIteration
                m = self.items[self.i]
                self.i += 1
                return m
        return _Iter()


@dataclass
class FakeFullGuild:
    id: int
    name: str = "Full Server"
    member_count: int = 3
    created_at: datetime = field(
        default_factory=lambda: datetime(2025, 1, 1, tzinfo=timezone.utc))
    owner: object = None
    channels: list = field(default_factory=list)
    roles: list = field(default_factory=list)
    members: list = field(default_factory=list)

    async def chunk(self):
        return True


def build_guild():
    g = FakeFullGuild(id=111)
    g.owner = FakeMember(id=333, name="owner")
    cat = FakeCategory(id=777)
    g.channels = [
        FakeHistoryChannel(id=222, guild=g, name="general", position=0,
                           category=cat, topic="hi"),
        FakeHistoryChannel(id=223, guild=g, name="no-category", position=1),
        FakeHistoryChannel(id=224, guild=g, name="voice", position=2,
                           type=discord.ChannelType.voice),
    ]
    g.roles = [
        FakeRole(id=900, name="Admin", position=3),
        FakeRole(id=901, name="Helper Bot", position=2, _bot_managed=True),
    ]
    g.members = [
        FakeMember(id=333, name="alice", display_name="Alice",
                   roles=[g.roles[0], g.roles[1]]),
        FakeMember(id=444, name="bot", display_name="Bot", bot=True),
    ]
    return g


class TestRoleHelpers(unittest.TestCase):
    def test_role_is_bot_managed(self):
        self.assertTrue(role_is_bot(FakeRole(id=1, _bot_managed=True)))
        self.assertFalse(role_is_bot(FakeRole(id=1)))

    def test_role_is_bot_is_defensive(self):
        """A real discord.Role has is_bot_managed; a stub may have neither."""
        class Bare:
            pass
        self.assertFalse(role_is_bot(Bare()))

    def test_category_id_is_null_when_absent(self):
        self.assertIsNone(channel_category_id(FakeHistoryChannel(id=1, guild=None)))
        self.assertEqual(
            channel_category_id(FakeHistoryChannel(id=1, guild=None,
                                                   category=FakeCategory(id=777))),
            "777")


class TestCatalogGuild(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.guild = build_guild()

    def tearDown(self):
        self.db.close()

    def test_catalog_guild_does_not_crash_on_real_role_api(self):
        """The production failure: Role has no is_bot()."""
        asyncio.run(backfill.catalog_guild(self.db, self.guild))

    def test_guild_row_written(self):
        asyncio.run(backfill.catalog_guild(self.db, self.guild))
        g = self.db.conn.execute("SELECT * FROM guilds WHERE id='111'").fetchone()
        self.assertEqual(g["name"], "Full Server")
        self.assertEqual(g["member_count"], 3)
        self.assertIn("owner_id", g["meta_json"])

    def test_channels_written_with_correct_category(self):
        asyncio.run(backfill.catalog_guild(self.db, self.guild))
        rows = {r["name"]: r for r in self.db.channels(111)}
        self.assertEqual(rows["general"]["category_id"], "777")
        self.assertIsNone(rows["no-category"]["category_id"],
                          "categoryless channel must be NULL, not '0'")
        self.assertEqual(rows["general"]["topic"], "hi")

    def test_roles_written_and_bot_flag_correct(self):
        asyncio.run(backfill.catalog_guild(self.db, self.guild))
        roles = {r["name"]: r for r in self.db.conn.execute(
            "SELECT * FROM roles WHERE guild_id='111'")}
        self.assertEqual(roles["Admin"]["is_bot"], 0)
        self.assertEqual(roles["Helper Bot"]["is_bot"], 1)
        self.assertEqual(roles["Admin"]["colour"], 0x3498DB)

    def test_members_and_role_links_written(self):
        asyncio.run(backfill.catalog_guild(self.db, self.guild))
        self.assertEqual(len(self.db.members(111)), 2)
        links = self.db.conn.execute(
            "SELECT COUNT(*) n FROM member_roles WHERE member_id='333'").fetchone()["n"]
        self.assertEqual(links, 2)
        bots = self.db.conn.execute(
            "SELECT is_bot FROM members WHERE id='444'").fetchone()["is_bot"]
        self.assertEqual(bots, 1)


class TestBackfillChannel(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.guild = build_guild()
        self.ch = self.guild.channels[0]
        # newest first, as Discord returns them
        self.ch.messages = [
            make_message(3000 + i, channel=self.ch, content=f"m{i}")
            for i in reversed(range(5))
        ]

    def tearDown(self):
        self.db.close()

    def test_walks_history_and_marks_complete(self):
        r = asyncio.run(backfill.backfill_channel(self.db, self.ch, batch=2))
        self.assertEqual(r["new"], 5)
        self.assertEqual(r["seen"], 5)
        self.assertEqual(self.db.stats("111")["messages"], 5)
        self.assertEqual(self.db.get_sync("222")["backfill_complete"], 1)

    def test_second_run_skips_completed_channel(self):
        asyncio.run(backfill.backfill_channel(self.db, self.ch, batch=2))
        r = asyncio.run(backfill.backfill_channel(self.db, self.ch, batch=2))
        self.assertEqual(r["skipped"], 1)
        self.assertEqual(self.db.stats("111")["messages"], 5, "must not duplicate")

    def test_interruption_does_not_mark_channel_complete(self):
        """The silent-data-loss bug: an interrupted channel was flagged done,
        so the next run skipped it and archived nothing."""
        stop = asyncio.Event()
        stop.set()  # interrupt immediately
        r = asyncio.run(backfill.backfill_channel(self.db, self.ch, batch=2, stop=stop))
        self.assertTrue(r.get("interrupted"))

        sync = self.db.get_sync("222")
        complete = sync["backfill_complete"] if sync else 0
        self.assertEqual(complete, 0, "interrupted channel must stay pending")

    def test_resume_after_interruption_completes_the_channel(self):
        stop = asyncio.Event()
        stop.set()
        asyncio.run(backfill.backfill_channel(self.db, self.ch, batch=2, stop=stop))
        partial = self.db.stats("111")["messages"]
        self.assertLess(partial, 5)

        r = asyncio.run(backfill.backfill_channel(self.db, self.ch, batch=2))
        self.assertEqual(self.db.stats("111")["messages"], 5, "resumed run lost messages")
        self.assertEqual(r["new"], 5 - partial)
        self.assertEqual(self.db.get_sync("222")["backfill_complete"], 1)


class TestBackfillGuild(unittest.TestCase):
    def test_voice_channels_are_skipped_and_forbidden_is_tolerated(self):
        db = Database(":memory:")
        try:
            g = build_guild()
            g.channels[0].messages = [make_message(4000, channel=g.channels[0])]
            g.channels[1].forbidden = True

            # Make the second channel raise Forbidden the way Discord would.
            real_history = g.channels[1].history

            def raising_history(*a, **kw):
                raise discord.Forbidden(
                    type("R", (), {"status": 403, "reason": "Missing Access"})(),
                    "Missing Access")
            g.channels[1].history = raising_history

            r = asyncio.run(backfill.backfill_guild(db, g, batch=10))
            walked = [x for x in r["results"] if not x.get("skipped")]
            names = {x.get("name") for x in walked}
            self.assertIn("general", names)
            self.assertNotIn("voice", names, "voice channels must be excluded")
            self.assertNotIn("no-category", names, "Forbidden must be skipped, not fatal")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
