"""Server-change capture.

Two complementary sources:

1. Live gateway events (channel/role/member create-update-delete, ban/unban).
   These fire as changes happen while the bot is listening and need no extra
   permission beyond what the bot already has.

2. The guild audit log, pulled once at the start of a listen session to catch
   changes that happened while the bot was offline. Needs VIEW_AUDIT_LOG.

Both funnel into the same audit_events table.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

import discord

from .db import Database, iso

log = logging.getLogger("archiver.audit")


def _target_type(action_name: str) -> str:
    a = action_name.lower()
    if "channel" in a or "overwrite" in a:
        return "channel"
    if "role" in a:
        return "role"
    if "ban" in a or "kick" in a or "member" in a:
        return "member"
    if "guild" in a:
        return "guild"
    return "other"


def diff(before: Any, after: Any, keys: list[str]) -> dict[str, Any]:
    """Return {key: [before, after]} for the keys that changed."""
    out: dict[str, Any] = {}
    for k in keys:
        b = getattr(before, k, None)
        a = getattr(after, k, None)
        # Normalise objects (roles, colours) to comparable scalars.
        bv = getattr(b, "value", b)
        av = getattr(a, "value", a)
        if isinstance(bv, (list, tuple)):
            bv = [getattr(x, "id", x) for x in bv]
        if isinstance(av, (list, tuple)):
            av = [getattr(x, "id", x) for x in av]
        if bv != av:
            out[k] = [bv, av]
    return out


def record_live(db: Database, guild_id, event: str, target_type: str,
                target_id=None, target_name=None, actor=None,
                changes: Mapping[str, Any] | None = None) -> None:
    db.add_audit_event(
        guild_id=str(guild_id),
        event=event,
        target_type=target_type,
        target_id=str(target_id) if target_id else None,
        target_name=target_name,
        actor_id=str(getattr(actor, "id", "")) or None,
        actor_name=getattr(actor, "name", None),
        before_json=None,
        after_json=json.dumps(changes) if changes else None,
        captured_at=None,
    )


def record_entry(db: Database, entry: discord.AuditLogEntry) -> None:
    actor = getattr(entry, "user", None)
    target = getattr(entry, "target", None)
    db.add_audit_event(
        guild_id=str(getattr(entry.guild, "id", 0) or 0),
        event=str(entry.action.name),
        target_type=_target_type(str(entry.action.name)),
        target_id=str(getattr(target, "id", "")) or None,
        target_name=(getattr(target, "name", None)
                     or getattr(target, "display_name", None)),
        actor_id=str(getattr(actor, "id", "")) or None,
        actor_name=getattr(actor, "name", None),
        before_json=None,
        after_json=json.dumps(entry.reason) if getattr(entry, "reason", None) else None,
        captured_at=iso(entry.created_at),
    )


async def pull_audit_logs(db: Database, guild: discord.Guild,
                          limit: int = 500) -> int:
    """Record audit-log entries newer than the stored cursor. Returns the count."""
    cursor = db.get_meta(f"audit_cursor:{guild.id}")
    recorded = 0
    newest = 0
    try:
        async for e in guild.audit_logs(limit=limit, oldest_first=True):
            if cursor and e.id <= int(cursor):
                continue
            record_entry(db, e)
            recorded += 1
            newest = max(newest, e.id)
    except discord.Forbidden:
        log.warning("no VIEW_AUDIT_LOG permission on %s - skipping audit pull",
                    guild.name)
        return 0
    except discord.HTTPException:
        log.exception("audit-log pull failed for %s", guild.name)
        return recorded

    if newest:
        db.set_meta(f"audit_cursor:{guild.id}", str(newest))
    if recorded:
        log.info("audit: recorded %d change(s) that happened while offline", recorded)
    return recorded
