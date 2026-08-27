"""Tests for the live-capture client's scope and loop-guard logic.

These exercise the decision logic in ArchiverClient without opening a gateway
connection, which is the part that needs a real token.
"""

from __future__ import annotations

import unittest

import discord

from archiver.db import Database
from archiver.listener import build_intents


class TestIntents(unittest.TestCase):
    def test_message_content_intent_is_requested(self):
        """Without this the archive fills with empty strings and no error."""
        i = build_intents()
        self.assertTrue(i.message_content)
        self.assertTrue(i.guilds)
        self.assertTrue(i.guild_messages)
        self.assertTrue(i.guild_reactions)

    def test_members_intent_is_optional(self):
        self.assertTrue(build_intents(members=True).members)
        self.assertFalse(build_intents(members=False).members)

    def test_intents_are_a_valid_discord_value(self):
        self.assertIsInstance(build_intents().value, int)


class TestScope(unittest.TestCase):
    """_in_scope decides what gets archived when several guilds are shared."""

    def setUp(self):
        self.db = Database(":memory:")

    def tearDown(self):
        self.db.close()

    def _client(self, guild_id):
        # Construct without calling __init__ so no gateway work happens.
        from archiver.listener import ArchiverClient
        c = ArchiverClient.__new__(ArchiverClient)
        c.target_guild_id = guild_id
        return c

    def test_restricted_client_accepts_only_target_guild(self):
        c = self._client(111)
        self.assertTrue(c._in_scope(111))
        self.assertFalse(c._in_scope(999))
        self.assertFalse(c._in_scope(None))

    def test_unrestricted_client_accepts_everything(self):
        c = self._client(None)
        self.assertTrue(c._in_scope(111))
        self.assertTrue(c._in_scope(999))


if __name__ == "__main__":
    unittest.main()
