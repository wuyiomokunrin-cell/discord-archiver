"""Resumable history backfill.

Walks every text channel from newest to oldest, checkpointing into sync_state
after each batch. If the process is killed mid-run, restarting picks up at the
last checkpoint instead of re-reading the whole channel.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord

from .capture import capture_message
from .db import Database

log = logging.getLogger("archiver.backfill")

# Channel types worth backfilling: text(0), announcement(5), forum(15).
BACKFILLABLE = (discord.ChannelType.text, discord.ChannelType.news, discord.ChannelType.forum)


async def catalog_guild(db: Database, guild: discord.Guild) -> None:
    """Snapshot the guild's channels, roles, and members."""
    db.upsert_guild(str(guild.id), guild.name, member_count=guild.member_count,
                    meta={"owner_id": str(getattr(guild.owner, "id", "")),
                          "created_at": guild.created_at.isoformat()})

    for ch in guild.channels:
        db.upsert_channel(
            channel_id=str(ch.id), guild_id=str(guild.id),
            name=getattr(ch, "name", None),
            type_=getattr(getattr(ch, "type", None), "value", None),
            position=getattr(ch, "position", None),
            category_id=str(getattr(getattr(ch, "category", None), "id", 0) or 0) or None,
            topic=getattr(ch, "topic", None),
            nsfw=bool(getattr(ch, "nsfw", False)),
        )

    for role in guild.roles:
        db.upsert_role(str(role.id), str(guild.id), role.name,
                       colour=role.colour.value, position=role.position,
                       is_bot=role.is_bot())

    for member in guild.members:
        db.upsert_member(
            member_id=str(member.id), guild_id=str(guild.id),
            name=member.name, display_name=member.display_name,
            is_bot=member.bot, joined_at=member.joined_at,
            avatar_url=str(member.display_avatar.url) if member.display_avatar else None,
            role_ids=[str(r.id) for r in member.roles],
        )
    log.info("catalogued %s: %d channels, %d roles, %d members",
             guild.name, len(guild.channels), len(guild.roles), len(guild.members))


async def backfill_channel(db: Database, channel: discord.TextChannel,
                           batch: int = 100,
                           stop: asyncio.Event | None = None) -> dict[str, int]:
    """Walk one channel's history. Resumable via the sync_state cursor."""
    cid = str(channel.id)
    sync = db.get_sync(cid)
    if sync and sync["backfill_complete"]:
        return {"channel": cid, "skipped": 1, "new": 0, "seen": 0}

    before = None
    if sync and sync["oldest_message_id"]:
        try:
            before = discord.Object(id=int(sync["oldest_message_id"]))
            log.info("resuming #%s from checkpoint %s", channel.name, before.id)
        except (TypeError, ValueError):
            before = None

    new = seen = 0
    buf: list[Any] = []

    async for msg in channel.history(limit=None, before=before, oldest_first=False):
        if stop and stop.is_set():
            log.warning("backfill of #%s interrupted by stop signal", channel.name)
            break
        buf.append(msg)
        if len(buf) >= batch:
            n = await _flush(db, buf)
            new += n
            seen += len(buf)
            db.set_backfill_cursor(cid, str(buf[-1].id), increment=n)
            log.info("#%s: %d messages (%d new)", channel.name, seen, new)
            buf = []

    if buf:
        n = await _flush(db, buf)
        new += n
        seen += len(buf)
        db.set_backfill_cursor(cid, str(buf[-1].id), increment=n)

    db.mark_backfill_complete(cid)
    log.info("#%s: complete. %d seen, %d new", channel.name, seen, new)
    return {"channel": cid, "name": channel.name, "new": new, "seen": seen, "skipped": 0}


async def _flush(db: Database, messages: list[Any]) -> int:
    new = 0
    for msg in messages:
        try:
            if capture_message(db, msg):
                new += 1
        except Exception:
            log.exception("failed to capture message %s", getattr(msg, "id", "?"))
    return new


async def backfill_guild(db: Database, guild: discord.Guild, batch: int = 100,
                         stop: asyncio.Event | None = None) -> dict[str, Any]:
    """Backfill every eligible channel in a guild."""
    await catalog_guild(db, guild)

    results = []
    targets = [c for c in guild.channels if c.type in BACKFILLABLE]
    log.info("backfilling %d channels in %s", len(targets), guild.name)

    for ch in targets:
        if stop and stop.is_set():
            break
        try:
            results.append(await backfill_channel(db, ch, batch=batch, stop=stop))
        except discord.Forbidden:
            log.warning("no permission to read history in #%s - skipping", ch.name)
        except discord.HTTPException:
            log.exception("HTTP error reading #%s - skipping", ch.name)

    return {"guild": guild.name, "channels": len(results), "results": results}
