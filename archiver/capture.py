"""Shared capture logic.

Both the live gateway listener and the history backfiller funnel messages
through `capture_message`, so a message is normalised identically no matter
which path it arrived by. That matters: history pagination and live events can
hand you the same message twice, and deduplication has to be airtight.
"""

from __future__ import annotations

from typing import Any

from .db import Database


def message_to_row(msg) -> dict[str, Any]:
    """Normalise a discord.Message into the dict shape Database.insert_message wants."""
    author = msg.author
    reply_to = None
    if getattr(msg, "reference", None) and msg.reference.message_id:
        reply_to = msg.reference.message_id

    guild_id = getattr(msg.guild, "id", None)
    if guild_id is None:
        # DM or ephemeral context - fall back to the channel's guild if present.
        guild_id = getattr(getattr(msg, "channel", None), "guild", None)
        guild_id = getattr(guild_id, "id", None)

    return {
        "id": msg.id,
        "channel_id": msg.channel.id,
        "guild_id": guild_id or 0,
        "author_id": getattr(author, "id", None),
        "author_name": getattr(author, "display_name", None) or getattr(author, "name", None),
        "author_is_bot": bool(getattr(author, "bot", False)),
        "content": msg.content or None,
        "timestamp": msg.created_at,
        "edited_timestamp": msg.edited_at,
        "type": getattr(msg.type, "value", msg.type),
        "pinned": bool(getattr(msg, "pinned", False)),
        "reply_to_message_id": reply_to,
        "embeds": [e.to_dict() for e in (msg.embeds or [])],
        "components": [],
        "raw": None,
    }


def ensure_channel(db: Database, msg) -> None:
    ch = msg.channel
    guild_id = getattr(getattr(ch, "guild", None), "id", None) or 0
    db.upsert_channel(
        channel_id=ch.id,
        guild_id=guild_id,
        name=getattr(ch, "name", None),
        type_=getattr(getattr(ch, "type", None), "value", None),
        position=getattr(ch, "position", None),
        category_id=getattr(getattr(ch, "category", None), "id", None),
        topic=getattr(ch, "topic", None),
        nsfw=bool(getattr(ch, "nsfw", False)),
    )


def ensure_author(db: Database, msg) -> None:
    author = msg.author
    if author is None or getattr(author, "id", None) is None:
        return
    guild = getattr(msg, "guild", None)
    role_ids = [r.id for r in getattr(author, "roles", [])] if guild else []
    db.upsert_member(
        member_id=author.id,
        guild_id=getattr(guild, "id", 0),
        name=getattr(author, "name", None),
        display_name=getattr(author, "display_name", None),
        is_bot=bool(getattr(author, "bot", False)),
        joined_at=getattr(author, "joined_at", None),
        avatar_url=getattr(author, "display_avatar", None) and str(author.display_avatar.url),
        role_ids=role_ids,
    )


def capture_message(db: Database, msg) -> bool:
    """Persist a message. Returns True if it was new.

    Ensures the parent channel and author exist first, so foreign keys hold
    even when a message arrives before its channel has been catalogued.
    """
    ensure_channel(db, msg)
    ensure_author(db, msg)

    row = message_to_row(msg)
    was_new = db.insert_message(row)

    # Attachments are recorded even for already-seen messages; the table dedupes
    # on primary key, so re-recording is harmless and self-healing.
    if getattr(msg, "attachments", None):
        from .attachments import record_attachments
        record_attachments(db, str(msg.id), str(msg.channel.id), msg.attachments)

    return was_new
