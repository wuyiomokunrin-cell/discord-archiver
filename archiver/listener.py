"""Live gateway capture.

Runs while the process is up. Because the process runs on your own machine and
you start it yourself, this is exactly the "online only when I'm online"
behaviour - no presence mirroring needed.
"""

from __future__ import annotations

import asyncio
import logging

import discord

from .capture import capture_message
from .db import Database

log = logging.getLogger("archiver.listener")


def build_intents(*, capture_edits: bool = True, capture_deletes: bool = True,
                  members: bool = True) -> discord.Intents:
    """Request the minimum intents the features actually need.

    MESSAGE_CONTENT is privileged. Without it (toggled in the Developer Portal
    *and* requested here) message.content arrives as an empty string and the
    archive silently fills with blanks.
    """
    intents = discord.Intents.default()
    intents.guilds = True
    intents.guild_messages = True
    intents.message_content = True
    intents.guild_reactions = True
    if members:
        intents.members = True  # privileged: needed for the member roster
    return intents


class ArchiverClient(discord.Client):
    def __init__(self, db: Database, *, guild_id: int | None = None,
                 capture_edits: bool = True, capture_deletes: bool = True,
                 mirror=None, **kwargs):
        super().__init__(**kwargs)
        self.db = db
        self.target_guild_id = guild_id
        self.capture_edits = capture_edits
        self.capture_deletes = capture_deletes
        self.mirror = mirror
        self.counters = {"messages": 0, "edits": 0, "deletes": 0, "reactions": 0, "skipped": 0}

    # ------------------------------------------------------------------ util

    def _in_scope(self, guild_id: int | None) -> bool:
        if self.target_guild_id is None:
            return True
        return guild_id == self.target_guild_id

    # ---------------------------------------------------------------- events

    async def on_ready(self) -> None:
        log.info("connected as %s (id=%s)", self.user, self.user.id)
        log.info("in %d guilds; archiving %s", len(self.guilds),
                 self.target_guild_id or "all guilds")

    async def on_message(self, message: discord.Message) -> None:
        if not self._in_scope(getattr(message.guild, "id", None)):
            self.counters["skipped"] += 1
            return
        # Never archive our own messages, and never re-mirror them (loop guard).
        if message.author.id == self.user.id:
            self.counters["skipped"] += 1
            return
        try:
            capture_message(self.db, message)
            self.counters["messages"] += 1
        except Exception:
            log.exception("failed to capture message %s", message.id)
            return

        if self.mirror is not None:
            try:
                await self.mirror.forward(message)
            except Exception:
                log.exception("mirror failed for message %s", message.id)

    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if not self.capture_edits:
            return
        if not self._in_scope(getattr(after.guild, "id", None)):
            return
        try:
            if self.db.record_edit(str(after.id), after.content or None, after.edited_at):
                self.counters["edits"] += 1
                log.debug("edit recorded for %s", after.id)
        except Exception:
            log.exception("failed to record edit for %s", after.id)

    async def on_message_delete(self, message: discord.Message) -> None:
        if not self.capture_deletes:
            return
        if not self._in_scope(getattr(message.guild, "id", None)):
            return
        try:
            self.db.mark_deleted(str(message.id))
            self.counters["deletes"] += 1
        except Exception:
            log.exception("failed to mark deletion for %s", message.id)

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        # Fires for messages that were not in the client cache - still worth
        # flagging them as deleted so the archive reflects reality.
        if not self.capture_deletes or payload.cached_message is not None:
            return
        try:
            self.db.mark_deleted(str(payload.message_id))
            self.counters["deletes"] += 1
        except Exception:
            log.exception("failed to mark raw deletion for %s", payload.message_id)

    async def on_raw_reaction_add(self, payload: discord.RawReactionAddEvent) -> None:
        # payload.guild_id is an int (or None in DMs) - no attribute unwrap needed.
        if not self._in_scope(payload.guild_id):
            return
        try:
            self.db.add_reaction(str(payload.message_id), str(payload.emoji),
                                 str(payload.user_id))
            self.counters["reactions"] += 1
        except Exception:
            log.exception("failed to record reaction on %s", payload.message_id)

    async def on_member_remove(self, member: discord.Member) -> None:
        if not self._in_scope(member.guild.id):
            return
        self.db.mark_member_left(str(member.id))


def make_client(db: Database, *, guild_id: int | None, capture_edits: bool,
                capture_deletes: bool, mirror=None, members: bool = True) -> ArchiverClient:
    return ArchiverClient(
        db,
        guild_id=guild_id,
        capture_edits=capture_edits,
        capture_deletes=capture_deletes,
        mirror=mirror,
        intents=build_intents(capture_edits=capture_edits,
                              capture_deletes=capture_deletes, members=members),
    )
