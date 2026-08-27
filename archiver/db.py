"""SQLite storage layer for the Discord archiver.

Snowflake IDs are stored as TEXT. They fit in a 64-bit int, but keeping them as
text avoids precision loss when the data is later exported to JSON and consumed
by JavaScript, where Number.MAX_SAFE_INTEGER is 2**53 - 1.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS guilds (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    member_count  INTEGER,
    captured_at   TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    meta_json     TEXT
);

CREATE TABLE IF NOT EXISTS channels (
    id           TEXT PRIMARY KEY,
    guild_id     TEXT NOT NULL REFERENCES guilds(id) ON DELETE CASCADE,
    name         TEXT,
    type         INTEGER,
    position     INTEGER,
    category_id  TEXT,
    topic        TEXT,
    nsfw         INTEGER NOT NULL DEFAULT 0,
    first_seen   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_channels_guild ON channels(guild_id, position);

CREATE TABLE IF NOT EXISTS roles (
    id        TEXT PRIMARY KEY,
    guild_id  TEXT NOT NULL REFERENCES guilds(id) ON DELETE CASCADE,
    name      TEXT,
    colour    INTEGER,
    position  INTEGER,
    is_bot    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS members (
    id            TEXT PRIMARY KEY,
    guild_id      TEXT NOT NULL REFERENCES guilds(id) ON DELETE CASCADE,
    name          TEXT,
    display_name  TEXT,
    is_bot        INTEGER NOT NULL DEFAULT 0,
    joined_at     TEXT,
    avatar_url    TEXT,
    first_seen    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    left_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_members_guild ON members(guild_id);

CREATE TABLE IF NOT EXISTS member_roles (
    member_id  TEXT NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    role_id    TEXT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    PRIMARY KEY (member_id, role_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id                 TEXT PRIMARY KEY,
    channel_id         TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    guild_id           TEXT NOT NULL REFERENCES guilds(id) ON DELETE CASCADE,
    author_id          TEXT,
    author_name        TEXT,
    author_is_bot      INTEGER NOT NULL DEFAULT 0,
    content            TEXT,
    timestamp          TEXT NOT NULL,
    edited_timestamp   TEXT,
    type               INTEGER,
    pinned             INTEGER NOT NULL DEFAULT 0,
    reply_to_message_id TEXT,
    embeds_json        TEXT,
    components_json    TEXT,
    deleted            INTEGER NOT NULL DEFAULT 0,
    deleted_at         TEXT,
    raw_json           TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_channel_time ON messages(channel_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_guild_time   ON messages(guild_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_author       ON messages(author_id);

CREATE TABLE IF NOT EXISTS attachments (
    id              TEXT PRIMARY KEY,
    message_id      TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    channel_id      TEXT,
    filename        TEXT,
    url             TEXT,
    local_path      TEXT,
    size            INTEGER,
    content_type    TEXT,
    width           INTEGER,
    height          INTEGER,
    sha256          TEXT,
    download_status TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_attachments_message ON attachments(message_id);
CREATE INDEX IF NOT EXISTS idx_attachments_pending ON attachments(download_status);
CREATE INDEX IF NOT EXISTS idx_attachments_sha     ON attachments(sha256);

CREATE TABLE IF NOT EXISTS message_edits (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id     TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    before_content TEXT,
    after_content  TEXT,
    captured_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edits_message ON message_edits(message_id);

CREATE TABLE IF NOT EXISTS message_deletes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id   TEXT NOT NULL,
    channel_id   TEXT,
    captured_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reactions (
    message_id  TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    emoji       TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    PRIMARY KEY (message_id, emoji, user_id)
);

CREATE TABLE IF NOT EXISTS sync_state (
    channel_id           TEXT PRIMARY KEY REFERENCES channels(id) ON DELETE CASCADE,
    oldest_message_id    TEXT,
    newest_message_id    TEXT,
    backfill_complete    INTEGER NOT NULL DEFAULT 0,
    backfill_message_count INTEGER NOT NULL DEFAULT 0,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def utcnow() -> str:
    """Current time as an ISO-8601 UTC string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def iso(dt: Any) -> Optional[str]:
    """Coerce a datetime (or None) to an ISO-8601 UTC string."""
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Thin wrapper around a SQLite connection with the archiver schema."""

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.init_schema()

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---------------------------------------------------------------- meta

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    # --------------------------------------------------------------- guild

    def upsert_guild(self, guild_id: str, name: str, member_count: int | None = None,
                     meta: Mapping[str, Any] | None = None) -> None:
        """Create or refresh a guild row.

        COALESCE on the conflicting columns: callers pass partial views of a
        guild. capture.ensure_guild() sees only what a gateway event carries, so
        without this it would blank out the member_count and meta_json that
        backfill.catalog_guild() had already filled in.
        """
        now = utcnow()
        self.conn.execute(
            """INSERT INTO guilds(id, name, member_count, captured_at, updated_at, meta_json)
               VALUES(?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 name = COALESCE(excluded.name, guilds.name),
                 member_count = COALESCE(excluded.member_count, guilds.member_count),
                 updated_at = excluded.updated_at,
                 meta_json = COALESCE(excluded.meta_json, guilds.meta_json)""",
            (str(guild_id), name, member_count, now, now,
             json.dumps(meta) if meta else None),
        )
        self.conn.commit()

    # ------------------------------------------------------------- channel

    def upsert_channel(self, channel_id: str, guild_id: str, name: str | None,
                       type_: int | None = None, position: int | None = None,
                       category_id: str | None = None, topic: str | None = None,
                       nsfw: bool = False) -> None:
        now = utcnow()
        self.conn.execute(
            """INSERT INTO channels(id, guild_id, name, type, position, category_id,
                                    topic, nsfw, first_seen, updated_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 name = excluded.name,
                 type = excluded.type,
                 position = excluded.position,
                 category_id = excluded.category_id,
                 topic = excluded.topic,
                 nsfw = excluded.nsfw,
                 updated_at = excluded.updated_at""",
            (str(channel_id), str(guild_id), name, type_, position,
             str(category_id) if category_id else None, topic, int(nsfw), now, now),
        )
        self.conn.commit()

    # ---------------------------------------------------------------- roles

    def upsert_role(self, role_id: str, guild_id: str, name: str | None,
                    colour: int | None = None, position: int | None = None,
                    is_bot: bool = False) -> None:
        self.conn.execute(
            """INSERT INTO roles(id, guild_id, name, colour, position, is_bot)
               VALUES(?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 name = excluded.name,
                 colour = excluded.colour,
                 position = excluded.position,
                 is_bot = excluded.is_bot""",
            (str(role_id), str(guild_id), name, colour, position, int(is_bot)),
        )
        self.conn.commit()

    # -------------------------------------------------------------- members

    def upsert_member(self, member_id: str, guild_id: str, name: str | None,
                      display_name: str | None = None, is_bot: bool = False,
                      joined_at: Any = None, avatar_url: str | None = None,
                      role_ids: Iterable[str] = ()) -> None:
        now = utcnow()
        self.conn.execute(
            """INSERT INTO members(id, guild_id, name, display_name, is_bot, joined_at,
                                   avatar_url, first_seen, updated_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 name = excluded.name,
                 display_name = excluded.display_name,
                 is_bot = excluded.is_bot,
                 joined_at = excluded.joined_at,
                 avatar_url = excluded.avatar_url,
                 updated_at = excluded.updated_at""",
            (str(member_id), str(guild_id), name, display_name, int(is_bot),
             iso(joined_at), avatar_url, now, now),
        )
        self.conn.execute("DELETE FROM member_roles WHERE member_id = ?", (str(member_id),))
        for rid in role_ids:
            self.conn.execute(
                "INSERT OR IGNORE INTO member_roles(member_id, role_id) VALUES(?, ?)",
                (str(member_id), str(rid)),
            )
        self.conn.commit()

    def mark_member_left(self, member_id: str) -> None:
        self.conn.execute(
            "UPDATE members SET left_at = ?, updated_at = ? WHERE id = ?",
            (utcnow(), utcnow(), str(member_id)),
        )
        self.conn.commit()

    # ------------------------------------------------------------- messages

    def insert_message(self, msg: Mapping[str, Any]) -> bool:
        """Insert a message row. Returns True if it was new, False if it already existed."""
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO messages(
                 id, channel_id, guild_id, author_id, author_name, author_is_bot,
                 content, timestamp, edited_timestamp, type, pinned,
                 reply_to_message_id, embeds_json, components_json, raw_json)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(msg["id"]),
                str(msg["channel_id"]),
                str(msg["guild_id"]),
                str(msg["author_id"]) if msg.get("author_id") else None,
                msg.get("author_name"),
                int(bool(msg.get("author_is_bot"))),
                msg.get("content"),
                iso(msg["timestamp"]),
                iso(msg.get("edited_timestamp")),
                msg.get("type"),
                int(bool(msg.get("pinned"))),
                str(msg["reply_to_message_id"]) if msg.get("reply_to_message_id") else None,
                json.dumps(msg["embeds"]) if msg.get("embeds") else None,
                json.dumps(msg["components"]) if msg.get("components") else None,
                json.dumps(msg["raw"]) if msg.get("raw") else None,
            ),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def get_message(self, message_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM messages WHERE id = ?", (str(message_id),)
        ).fetchone()

    def mark_deleted(self, message_id: str) -> None:
        now = utcnow()
        self.conn.execute(
            "UPDATE messages SET deleted = 1, deleted_at = ? WHERE id = ? AND deleted = 0",
            (now, str(message_id)),
        )
        self.conn.execute(
            "INSERT INTO message_deletes(message_id, channel_id, captured_at) "
            "SELECT ?, channel_id, ? FROM messages WHERE id = ?",
            (str(message_id), now, str(message_id)),
        )
        self.conn.commit()

    def record_edit(self, message_id: str, after_content: str | None,
                    after_edited_at: Any = None) -> bool:
        """Record an edit. Returns True if the content actually changed."""
        row = self.get_message(message_id)
        if row is None:
            return False
        before = row["content"]
        if before == after_content:
            return False
        self.conn.execute(
            "INSERT INTO message_edits(message_id, before_content, after_content, captured_at) "
            "VALUES(?, ?, ?, ?)",
            (str(message_id), before, after_content, utcnow()),
        )
        self.conn.execute(
            "UPDATE messages SET content = ?, edited_timestamp = ? WHERE id = ?",
            (after_content, iso(after_edited_at) or utcnow(), str(message_id)),
        )
        self.conn.commit()
        return True

    def add_reaction(self, message_id: str, emoji: str, user_id: str) -> bool:
        """Record a reaction. Returns False if the message was never archived.

        reactions.message_id has a foreign key, and on_raw_reaction_add fires
        for any message in the guild - including old ones the backfill has not
        reached. Inserting blindly would raise IntegrityError.
        """
        if self.get_message(message_id) is None:
            return False
        self.conn.execute(
            "INSERT OR IGNORE INTO reactions(message_id, emoji, user_id, captured_at) "
            "VALUES(?, ?, ?, ?)",
            (str(message_id), emoji, str(user_id), utcnow()),
        )
        self.conn.commit()
        return True

    # ---------------------------------------------------------- attachments

    def add_attachment(self, att: Mapping[str, Any]) -> bool:
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO attachments(
                 id, message_id, channel_id, filename, url, local_path, size,
                 content_type, width, height, sha256, download_status)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(att["id"]),
                str(att["message_id"]),
                str(att["channel_id"]) if att.get("channel_id") else None,
                att.get("filename"),
                att.get("url"),
                att.get("local_path"),
                att.get("size"),
                att.get("content_type"),
                att.get("width"),
                att.get("height"),
                att.get("sha256"),
                att.get("download_status", "pending"),
            ),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def pending_attachments(self, limit: int = 500) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM attachments WHERE download_status = 'pending' "
            "ORDER BY rowid LIMIT ?",
            (limit,),
        ).fetchall()

    def set_attachment_downloaded(self, att_id: str, local_path: str,
                                  sha256: str, size: int) -> None:
        self.conn.execute(
            "UPDATE attachments SET local_path = ?, sha256 = ?, size = ?, "
            "download_status = 'done' WHERE id = ?",
            (local_path, sha256, size, str(att_id)),
        )
        self.conn.commit()

    def set_attachment_failed(self, att_id: str) -> None:
        self.conn.execute(
            "UPDATE attachments SET download_status = 'failed' WHERE id = ?",
            (str(att_id),),
        )
        self.conn.commit()

    def find_attachment_by_sha(self, sha256: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM attachments WHERE sha256 = ? AND download_status = 'done' LIMIT 1",
            (sha256,),
        ).fetchone()

    # --------------------------------------------------------- sync cursors

    def get_sync(self, channel_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM sync_state WHERE channel_id = ?", (str(channel_id),)
        ).fetchone()

    def set_backfill_cursor(self, channel_id: str, oldest_message_id: str,
                            increment: int = 0, complete: bool = False) -> None:
        self.conn.execute(
            """INSERT INTO sync_state(channel_id, oldest_message_id, backfill_complete,
                                      backfill_message_count, updated_at)
               VALUES(?, ?, ?, ?, ?)
               ON CONFLICT(channel_id) DO UPDATE SET
                 oldest_message_id = excluded.oldest_message_id,
                 backfill_complete = excluded.backfill_complete,
                 backfill_message_count = sync_state.backfill_message_count + excluded.backfill_message_count,
                 updated_at = excluded.updated_at""",
            (str(channel_id), str(oldest_message_id), int(complete), increment, utcnow()),
        )
        self.conn.commit()

    def mark_backfill_complete(self, channel_id: str) -> None:
        self.conn.execute(
            "INSERT INTO sync_state(channel_id, backfill_complete, updated_at) "
            "VALUES(?, 1, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET backfill_complete = 1, "
            "updated_at = excluded.updated_at",
            (str(channel_id), utcnow()),
        )
        self.conn.commit()

    def channels_needing_backfill(self, guild_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT c.* FROM channels c
               LEFT JOIN sync_state s ON s.channel_id = c.id
               WHERE c.guild_id = ?
                 AND c.type IN (0, 5, 15)
                 AND (s.backfill_complete IS NULL OR s.backfill_complete = 0)
               ORDER BY c.position""",
            (str(guild_id),),
        ).fetchall()

    # -------------------------------------------------------------- queries

    def iter_messages(self, channel_id: str | None = None,
                      guild_id: str | None = None,
                      include_deleted: bool = True) -> Iterator[sqlite3.Row]:
        sql = "SELECT * FROM messages WHERE 1=1"
        params: list[Any] = []
        if channel_id is not None:
            sql += " AND channel_id = ?"
            params.append(str(channel_id))
        if guild_id is not None:
            sql += " AND guild_id = ?"
            params.append(str(guild_id))
        if not include_deleted:
            sql += " AND deleted = 0"
        sql += " ORDER BY id ASC"
        yield from self.conn.execute(sql, params)

    def attachments_for(self, message_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM attachments WHERE message_id = ?", (str(message_id),)
        ).fetchall()

    def edits_for(self, message_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM message_edits WHERE message_id = ? ORDER BY id",
            (str(message_id),),
        ).fetchall()

    def reactions_for(self, message_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM reactions WHERE message_id = ?", (str(message_id),)
        ).fetchall()

    def channels(self, guild_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM channels WHERE guild_id = ? ORDER BY position, id",
            (str(guild_id),),
        ).fetchall()

    def members(self, guild_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM members WHERE guild_id = ? ORDER BY name",
            (str(guild_id),),
        ).fetchall()

    def stats(self, guild_id: str | None = None) -> dict[str, Any]:
        where = ""
        params: list[Any] = []
        if guild_id is not None:
            where = "WHERE guild_id = ?"
            params.append(str(guild_id))
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n, SUM(deleted) AS d FROM messages {where}", params
        ).fetchone()
        att = self.conn.execute(
            """SELECT COUNT(*) AS n,
                      SUM(CASE WHEN download_status='done' THEN 1 ELSE 0 END) AS done,
                      SUM(CASE WHEN download_status='done' THEN size ELSE 0 END) AS bytes
               FROM attachments"""
        ).fetchone()
        edits = self.conn.execute("SELECT COUNT(*) AS n FROM message_edits").fetchone()
        return {
            "messages": row["n"] or 0,
            "deleted_messages": row["d"] or 0,
            "attachments": att["n"] or 0,
            "attachments_downloaded": att["done"] or 0,
            "attachment_bytes": att["bytes"] or 0,
            "edits": edits["n"] or 0,
        }
