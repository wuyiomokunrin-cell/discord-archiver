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

from .capture import (capture_message, channel_category_id, channel_parent_id,
                      role_is_bot)
from .db import Database

log = logging.getLogger("archiver.backfill")

# Channel types worth backfilling. Forum(15) is included but handled
# separately: a ForumChannel has no history() of its own, only threads.
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
            category_id=channel_category_id(ch),
            parent_id=channel_parent_id(ch),
            topic=getattr(ch, "topic", None),
            nsfw=bool(getattr(ch, "nsfw", False)),
        )

    # guild.channels excludes threads; record them too so the dashboard can
    # nest each thread under its parent channel, Discord-style.
    for th in getattr(guild, "threads", []) or []:
        db.upsert_channel(
            channel_id=str(th.id), guild_id=str(guild.id),
            name=getattr(th, "name", None),
            type_=getattr(getattr(th, "type", None), "value", None),
            position=getattr(th, "position", None),
            category_id=channel_category_id(th),
            parent_id=channel_parent_id(th),
            topic=getattr(th, "topic", None),
            nsfw=bool(getattr(th, "nsfw", False)),
        )

    for role in guild.roles:
        db.upsert_role(str(role.id), str(guild.id), role.name,
                       colour=role.colour.value, position=role.position,
                       is_bot=role_is_bot(role))

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

    # Every table here hangs off guilds, and sync_state hangs off channels.
    # Ensure both parents exist before anything is written against them, so
    # this is safe to call without catalog_guild having run first.
    guild = getattr(channel, "guild", None)
    guild_id = str(getattr(guild, "id", 0) or 0)
    db.upsert_guild(guild_id, getattr(guild, "name", None) or guild_id)
    db.upsert_channel(
        channel_id=cid,
        guild_id=str(getattr(getattr(channel, "guild", None), "id", 0) or 0),
        name=getattr(channel, "name", None),
        type_=getattr(getattr(channel, "type", None), "value", None),
        position=getattr(channel, "position", None),
        category_id=channel_category_id(channel),
        parent_id=channel_parent_id(channel),
        topic=getattr(channel, "topic", None),
        nsfw=bool(getattr(channel, "nsfw", False)),
    )

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
    interrupted = False
    buf: list[Any] = []

    async for msg in channel.history(limit=None, before=before, oldest_first=False):
        if stop and stop.is_set():
            log.warning("backfill of #%s interrupted by stop signal", channel.name)
            interrupted = True
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

    # Only a fully-walked channel may be flagged complete. Marking an
    # interrupted one would make the next run skip it, silently archiving
    # nothing - and sync_state has a foreign key to channels, which does not
    # exist yet if the run stopped before the first message.
    if interrupted:
        log.info("#%s: paused after %d seen (%d new); will resume", channel.name, seen, new)
        return {"channel": cid, "name": channel.name, "new": new, "seen": seen,
                "skipped": 0, "interrupted": True}

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


async def collect_forum_threads(forum) -> list:
    """Active plus archived threads in a forum, deduplicated by id."""
    found: dict[int, Any] = {}
    for t in (getattr(forum, "threads", None) or []):
        found[t.id] = t
    try:
        async for t in forum.archived_threads(limit=None):
            found[t.id] = t
    except discord.Forbidden:
        log.warning("no permission to list archived threads in #%s", forum.name)
    except discord.HTTPException:
        log.exception("could not list archived threads in #%s", forum.name)
    return list(found.values())


async def backfill_forum(db: Database, forum, batch: int = 100,
                         stop: asyncio.Event | None = None) -> dict[str, Any]:
    """Walk every thread in a forum. The forum itself holds no messages."""
    cid = str(forum.id)
    guild = getattr(forum, "guild", None)
    db.upsert_guild(str(getattr(guild, "id", 0) or 0),
                    getattr(guild, "name", None) or "0")
    db.upsert_channel(
        channel_id=cid, guild_id=str(getattr(guild, "id", 0) or 0),
        name=getattr(forum, "name", None),
        type_=getattr(getattr(forum, "type", None), "value", None),
        position=getattr(forum, "position", None),
        category_id=channel_category_id(forum),
        topic=getattr(forum, "topic", None),
        nsfw=bool(getattr(forum, "nsfw", False)),
    )

    threads = await collect_forum_threads(forum)
    log.info("#%s: forum with %d thread(s)", forum.name, len(threads))

    interrupted = False
    sub: list[dict[str, Any]] = []
    for t in threads:
        if stop and stop.is_set():
            interrupted = True
            break
        try:
            sub.append(await backfill_channel(db, t, batch=batch, stop=stop))
        except discord.Forbidden:
            log.warning("no permission to read thread %s - skipping", t.name)
        except discord.HTTPException:
            log.exception("HTTP error reading thread %s - skipping", t.name)
        if stop and stop.is_set():
            interrupted = True
            break

    if not interrupted:
        db.mark_backfill_complete(cid)

    return {
        "channel": cid, "name": forum.name, "forum": True,
        "threads": len(threads), "results": sub,
        "new": sum(r.get("new", 0) for r in sub),
        "seen": sum(r.get("seen", 0) for r in sub),
        "skipped": 0, "interrupted": interrupted,
    }


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
        if ch.type == discord.ChannelType.forum:
            results.append(await backfill_forum(db, ch, batch=batch, stop=stop))
            continue
        if not hasattr(ch, "history"):
            # Defensive: an unfamiliar channel type must not abort the run.
            # This is what would have caught the ForumChannel crash earlier.
            log.warning("#%s is a %s with no history(); skipping",
                        ch.name, getattr(ch.type, "name", ch.type))
            continue
        try:
            results.append(await backfill_channel(db, ch, batch=batch, stop=stop))
        except discord.Forbidden:
            log.warning("no permission to read history in #%s - skipping", ch.name)
        except discord.HTTPException:
            log.exception("HTTP error reading #%s - skipping", ch.name)

    return {"guild": guild.name, "channels": len(results), "results": results}
