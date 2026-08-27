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


def ensure_guild(db: Database, msg) -> str | None:
    """Make sure the guild row exists. Returns the guild id, or None for DMs.

    This must run before ensure_channel and ensure_author: both tables carry a
    foreign key to guilds, so inserting either without the parent row raises
    IntegrityError. That happens in practice because the gateway starts
    delivering MESSAGE_CREATE the moment the client connects, well before
    backfill.catalog_guild() has had a chance to run.

    Results are cached per-Database so a busy server does not cost a write per
    message.
    """
    guild = getattr(msg, "guild", None)
    guild_id = getattr(guild, "id", None)
    if guild_id is None:
        return None

    key = str(guild_id)
    seen = getattr(db, "_seen_guilds", None)
    if seen is None:
        seen = db._seen_guilds = set()
    if key in seen:
        return key

    db.upsert_guild(key, getattr(guild, "name", None) or key,
                    member_count=getattr(guild, "member_count", None))
    seen.add(key)
    return key


def ensure_channel(db: Database, msg) -> bool:
    """Catalogue the channel. Returns False if it has no guild (DM)."""
    ch = msg.channel
    guild_id = ensure_guild(db, msg)
    if guild_id is None:
        return False

    key = str(ch.id)
    seen = getattr(db, "_seen_channels", None)
    if seen is None:
        seen = db._seen_channels = set()
    if key in seen:
        return True

    db.upsert_channel(
        channel_id=key,
        guild_id=guild_id,
        name=getattr(ch, "name", None),
        type_=getattr(getattr(ch, "type", None), "value", None),
        position=getattr(ch, "position", None),
        category_id=getattr(getattr(ch, "category", None), "id", None),
        topic=getattr(ch, "topic", None),
        nsfw=bool(getattr(ch, "nsfw", False)),
    )
    seen.add(key)
    return True


def ensure_roles(db: Database, msg) -> list[str]:
    """Catalogue the author's roles and return their ids.

    member_roles.role_id references roles(id), so every role has to exist
    before the link can be written. Roles are otherwise only created by
    backfill.catalog_guild(), which has not necessarily run yet - this is the
    same race that broke ensure_channel, one level down.
    """
    guild = getattr(msg, "guild", None)
    if guild is None:
        return []

    seen = getattr(db, "_seen_roles", None)
    if seen is None:
        seen = db._seen_roles = set()

    ids: list[str] = []
    for role in (getattr(msg.author, "roles", None) or []):
        rid = getattr(role, "id", None)
        if rid is None:
            continue
        key = str(rid)
        if key not in seen:
            is_bot = getattr(role, "is_bot", None)
            db.upsert_role(
                key, str(guild.id), getattr(role, "name", None),
                colour=getattr(getattr(role, "colour", None), "value", None),
                position=getattr(role, "position", None),
                is_bot=bool(is_bot()) if callable(is_bot) else False,
            )
            seen.add(key)
        ids.append(key)
    return ids


def ensure_author(db: Database, msg) -> None:
    author = msg.author
    if author is None or getattr(author, "id", None) is None:
        return
    guild_id = ensure_guild(db, msg)
    if guild_id is None:
        return  # DM: no guild row to hang the member off
    role_ids = ensure_roles(db, msg)
    db.upsert_member(
        member_id=author.id,
        guild_id=guild_id,
        name=getattr(author, "name", None),
        display_name=getattr(author, "display_name", None),
        is_bot=bool(getattr(author, "bot", False)),
        joined_at=getattr(author, "joined_at", None),
        avatar_url=getattr(author, "display_avatar", None) and str(author.display_avatar.url),
        role_ids=role_ids,
    )


def capture_message(db: Database, msg) -> bool:
    """Persist a message. Returns True if it was new, False if already stored
    or not storable.

    Creates the guild, channel, and author rows on demand, in that order, so
    foreign keys hold even when a message arrives before anything has been
    catalogued. Returns False for DMs: every table in the schema hangs off a
    guild, so there is nowhere to put them.
    """
    if not ensure_channel(db, msg):
        return False
    ensure_author(db, msg)

    row = message_to_row(msg)
    was_new = db.insert_message(row)

    # Attachments are recorded even for already-seen messages; the table dedupes
    # on primary key, so re-recording is harmless and self-healing.
    if getattr(msg, "attachments", None):
        from .attachments import record_attachments
        record_attachments(db, str(msg.id), str(msg.channel.id), msg.attachments)

    return was_new
