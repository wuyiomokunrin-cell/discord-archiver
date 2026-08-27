"""Export layer.

The "template" is a declarative field spec (EXPORT_SCHEMA) that drives every
format. Add a field there and it appears in JSON, CSV, and the HTML export
without touching the renderers.
"""

from __future__ import annotations

import csv
import html
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .db import Database


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    source: str  # key in the flattened message dict
    in_json: bool = True
    in_csv: bool = True
    in_html: bool = True


# The single source of truth for what an exported message looks like.
EXPORT_SCHEMA: list[Field] = (
    Field("id", "Message ID", "id"),
    Field("channel", "Channel", "channel"),
    Field("author", "Author", "author"),
    Field("author_id", "Author ID", "author_id", in_html=False),
    Field("timestamp", "Timestamp", "timestamp"),
    Field("content", "Content", "content"),
    Field("edited", "Edited", "edited_timestamp", in_html=False),
    Field("reply_to", "Reply to", "reply_to_message_id", in_html=False),
    Field("pinned", "Pinned", "pinned", in_html=False),
    Field("deleted", "Deleted", "deleted", in_html=False),
    Field("attachments", "Attachments", "attachments"),
    Field("embeds", "Embeds", "embeds", in_csv=False, in_html=False),
    Field("edits", "Edit history", "edits", in_csv=False, in_html=False),
    Field("reactions", "Reactions", "reactions"),
    Field("type", "Type", "type", in_csv=False, in_html=False),
)

CSV_FIELDS = [f for f in EXPORT_SCHEMA if f.in_csv]
JSON_FIELDS = [f for f in EXPORT_SCHEMA if f.in_json]


def flatten_message(db: Database, row) -> dict[str, Any]:
    """Turn a messages table row into the export-shaped dict."""
    atts = db.attachments_for(row["id"])
    return {
        "id": row["id"],
        "channel": _channel_name(db, row["channel_id"]),
        "channel_id": row["channel_id"],
        "guild_id": row["guild_id"],
        "author": row["author_name"] or ("Unknown" if not row["author_id"] else row["author_id"]),
        "author_id": row["author_id"],
        "timestamp": row["timestamp"],
        "content": row["content"] or "",
        "edited_timestamp": row["edited_timestamp"],
        "reply_to_message_id": row["reply_to_message_id"],
        "pinned": bool(row["pinned"]),
        "deleted": bool(row["deleted"]),
        "type": row["type"],
        "attachments": [
            {
                "filename": a["filename"],
                "original_url": a["url"],
                "local_path": a["local_path"],
                "size": a["size"],
                "content_type": a["content_type"],
                "downloaded": a["download_status"] == "done",
            }
            for a in atts
        ],
        "embeds": json.loads(row["embeds_json"]) if row["embeds_json"] else [],
        "edits": [
            {"before": e["before_content"], "after": e["after_content"],
             "captured_at": e["captured_at"]}
            for e in db.edits_for(row["id"])
        ],
        "reactions": [
            {"emoji": r["emoji"], "user_id": r["user_id"]}
            for r in db.reactions_for(row["id"])
        ],
    }


_channel_cache: dict[str, str] = {}


def _channel_name(db: Database, channel_id: str) -> str:
    if channel_id in _channel_cache:
        return _channel_cache[channel_id]
    row = db.conn.execute("SELECT name FROM channels WHERE id = ?", (channel_id,)).fetchone()
    name = row["name"] if row and row["name"] else channel_id
    _channel_cache[channel_id] = name
    return name


def reset_channel_cache() -> None:
    _channel_cache.clear()


def _pick(fields: Iterable[Field], flat: dict[str, Any]) -> dict[str, Any]:
    return {f.name: flat.get(f.source) for f in fields}


# ------------------------------------------------------------------ JSON


def export_json(db: Database, out_path: str | Path, guild_id: str | None = None,
                channel_id: str | None = None, include_deleted: bool = True) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    reset_channel_cache()

    records = [
        _pick(JSON_FIELDS, flatten_message(db, row))
        for row in db.iter_messages(channel_id=channel_id, guild_id=guild_id,
                                    include_deleted=include_deleted)
    ]
    payload = {
        "generator": "discord-archiver",
        "schema_version": 1,
        "guild_id": guild_id,
        "channel_id": channel_id,
        "message_count": len(records),
        "messages": records,
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return out_path


# ------------------------------------------------------------------- CSV


def export_csv(db: Database, out_path: str | Path, guild_id: str | None = None,
               channel_id: str | None = None, include_deleted: bool = True) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    reset_channel_cache()

    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=[f.name for f in CSV_FIELDS],
                                extrasaction="ignore")
        writer.writeheader()
        for row in db.iter_messages(channel_id=channel_id, guild_id=guild_id,
                                    include_deleted=include_deleted):
            flat = flatten_message(db, row)
            rec = _pick(CSV_FIELDS, flat)
            # Flatten nested lists into readable strings for spreadsheet use.
            rec["attachments"] = "; ".join(
                a["local_path"] or a["original_url"] or "?" for a in flat["attachments"]
            )
            rec["reactions"] = "; ".join(
                f'{r["emoji"]}:{r["user_id"]}' for r in flat["reactions"]
            )
            writer.writerow(rec)
    return out_path


# ------------------------------------------------------------------ HTML

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 0; background: #1e1f22; color: #dbdee1; }}
  header {{ padding: 16px 24px; background: #2b2d31; border-bottom: 1px solid #1a1b1e; }}
  h1 {{ margin: 0; font-size: 18px; }}
  .meta {{ color: #949ba4; font-size: 13px; margin-top: 4px; }}
  .msg {{ display: grid; grid-template-columns: 160px 1fr; gap: 12px;
          padding: 8px 24px; border-bottom: 1px solid #2b2d31; }}
  .msg:hover {{ background: #232428; }}
  .who {{ color: #949ba4; font-size: 12px; }}
  .who b {{ display: block; color: #f2f3f5; font-size: 13px; }}
  .body {{ min-width: 0; }}
  .content {{ white-space: pre-wrap; word-break: break-word; }}
  .att {{ margin-top: 4px; }}
  .att a {{ color: #00a8fc; font-size: 13px; text-decoration: none; }}
  .img {{ max-width: 400px; max-height: 240px; border-radius: 4px; display: block; margin-top: 4px; }}
  .react {{ color: #949ba4; font-size: 12px; margin-top: 4px; }}
  .badge {{ display: inline-block; font-size: 11px; padding: 1px 6px; border-radius: 3px;
            margin-left: 6px; vertical-align: middle; }}
  .deleted {{ background: #5c1f1f; color: #f5b5b5; }}
  .edited {{ background: #4a3f1f; color: #f0dfa5; }}
  .daybreak {{ padding: 6px 24px; background: #2b2d31; color: #949ba4; font-size: 12px;
               position: sticky; top: 0; }}
</style>
</head>
<body>
<header>
  <h1>{title}</h1>
  <div class="meta">{subtitle}</div>
</header>
{body}
</body>
</html>
"""


def _attachment_html(att: dict[str, Any], base_dir: Path) -> str:
    label = html.escape(att["filename"] or "attachment")
    local = att.get("local_path")
    if local:
        try:
            rel = Path(local).resolve().relative_to(base_dir.resolve())
        except ValueError:
            rel = Path(local).name
        href = html.escape(str(rel).replace("\\", "/"))
    else:
        href = html.escape(att.get("original_url") or "#")
    is_img = (att.get("content_type") or "").startswith("image/")
    if is_img:
        return f'<a href="{href}"><img class="img" src="{href}" alt="{label}"></a>'
    return f'<div class="att"><a href="{href}">{label}</a></div>'


def export_html(db: Database, out_path: str | Path, guild_id: str | None = None,
                channel_id: str | None = None, include_deleted: bool = True,
                title: str | None = None) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    reset_channel_cache()

    parts: list[str] = []
    last_day = None
    count = 0

    for row in db.iter_messages(channel_id=channel_id, guild_id=guild_id,
                                include_deleted=include_deleted):
        flat = flatten_message(db, row)
        count += 1
        day = (flat["timestamp"] or "")[:10]
        if day != last_day:
            parts.append(f'<div class="daybreak">{html.escape(day)}</div>')
            last_day = day

        badges = ""
        if flat["deleted"]:
            badges += '<span class="badge deleted">deleted</span>'
        if flat["edited_timestamp"]:
            badges += '<span class="badge edited">edited</span>'

        atts = "".join(_attachment_html(a, out_path.parent) for a in flat["attachments"])
        reacts = ""
        if flat["reactions"]:
            grouped: dict[str, int] = {}
            for r in flat["reactions"]:
                grouped[r["emoji"]] = grouped.get(r["emoji"], 0) + 1
            reacts = '<div class="react">' + html.escape(
                "  ".join(f"{e} {n}" for e, n in grouped.items())
            ) + "</div>"

        ts = html.escape((flat["timestamp"] or "")[11:16])
        parts.append(
            f'<div class="msg">'
            f'<div class="who"><b>{html.escape(flat["author"])}</b>{ts}</div>'
            f'<div class="body"><div class="content">{html.escape(flat["content"])}'
            f"{badges}</div>{atts}{reacts}</div></div>"
        )

    subtitle = f"{count} message{'s' if count != 1 else ''}"
    if channel_id:
        subtitle += f" &middot; #{html.escape(_channel_name(db, channel_id))}"
    if not include_deleted:
        subtitle += " &middot; deleted messages hidden"

    out_path.write_text(
        HTML_PAGE.format(
            title=html.escape(title or "Discord archive"),
            subtitle=subtitle,
            body="\n".join(parts),
        ),
        encoding="utf-8",
    )
    return out_path


# -------------------------------------------------------------- one-shot


def export_all(db: Database, out_dir: str | Path, guild_id: str | None = None,
               channel_id: str | None = None, include_deleted: bool = True,
               title: str | None = None) -> dict[str, Path]:
    """Write JSON + CSV + HTML side by side. Returns the paths written."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = channel_id or guild_id or "archive"
    return {
        "json": export_json(db, out_dir / f"{stem}.json", guild_id, channel_id, include_deleted),
        "csv": export_csv(db, out_dir / f"{stem}.csv", guild_id, channel_id, include_deleted),
        "html": export_html(db, out_dir / f"{stem}.html", guild_id, channel_id,
                            include_deleted, title),
    }


# ---------------------------------------------------- server-info report


def server_info(db: Database, guild_id: str) -> dict[str, Any]:
    """The 'server info' payload: guild, channels, members, roles, counts."""
    g = db.conn.execute("SELECT * FROM guilds WHERE id = ?", (guild_id,)).fetchone()
    if g is None:
        raise KeyError(f"guild {guild_id} not found in archive")

    channels = db.channels(guild_id)
    members = db.members(guild_id)
    roles = db.conn.execute(
        "SELECT * FROM roles WHERE guild_id = ? ORDER BY position DESC", (guild_id,)
    ).fetchall()

    per_channel = []
    for c in channels:
        n = db.conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE channel_id = ?", (c["id"],)
        ).fetchone()["n"]
        per_channel.append({
            "id": c["id"], "name": c["name"], "type": c["type"],
            "topic": c["topic"], "nsfw": bool(c["nsfw"]), "messages": n,
        })

    return {
        "guild": {
            "id": g["id"], "name": g["name"], "member_count": g["member_count"],
            "captured_at": g["captured_at"], "updated_at": g["updated_at"],
        },
        "channels": per_channel,
        "members": [
            {"id": m["id"], "name": m["name"], "display_name": m["display_name"],
             "is_bot": bool(m["is_bot"]), "joined_at": m["joined_at"],
             "left_at": m["left_at"]}
            for m in members
        ],
        "roles": [
            {"id": r["id"], "name": r["name"], "colour": r["colour"],
             "position": r["position"]}
            for r in roles
        ],
        "totals": db.stats(guild_id),
    }


def export_server_info(db: Database, guild_id: str, out_dir: str | Path) -> dict[str, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    info = server_info(db, guild_id)
    json_path = out_dir / "server-info.json"
    json_path.write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")

    members_csv = out_dir / "members.csv"
    with members_csv.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["id", "name", "display_name", "is_bot",
                                           "joined_at", "left_at"])
        w.writeheader()
        for m in info["members"]:
            w.writerow(m)

    channels_csv = out_dir / "channels.csv"
    with channels_csv.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["id", "name", "type", "topic", "nsfw", "messages"])
        w.writeheader()
        for c in info["channels"]:
            w.writerow(c)

    roles_csv = out_dir / "roles.csv"
    with roles_csv.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["id", "name", "colour", "position"])
        w.writeheader()
        for r in info["roles"]:
            w.writerow(r)

    return {"json": json_path, "members_csv": members_csv,
            "channels_csv": channels_csv, "roles_csv": roles_csv}
