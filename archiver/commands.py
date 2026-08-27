"""Slash commands so admins can query the archive from Discord chat.

The dashboard is local (127.0.0.1) and cannot be shown to other people, so these
commands surface the *same data* directly in Discord. They read the local SQLite
archive - nothing here writes to it and nothing opens a terminal.

All data commands require Manage Server or Administrator; ``/archive help`` is
available to everyone so people can discover what exists.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from .db import THREAD_TYPES

log = logging.getLogger(__name__)

GROUP = "archive"
_EMBED_LIMIT = 5500  # stay under Discord's 6000-char embed total


def _is_mod(interaction: discord.Interaction) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms) and (perms.administrator or perms.manage_guild)


mod_only = app_commands.check(_is_mod)


def _embed(title: str, colour: int = 0x00A8FC) -> discord.Embed:
    return discord.Embed(title=title, colour=colour)


def _trunc(s: str | None, n: int = 300) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def setup_commands(client: discord.Client) -> None:
    """Attach the /archive command group to the client's tree. Call once."""
    db = client.db
    # A plain discord.Client has no .tree; constructing CommandTree(client)
    # registers it with the gateway state so interactions route to it.
    tree = getattr(client, "tree", None)
    if tree is None:
        tree = app_commands.CommandTree(client)
        client.tree = tree
    group = app_commands.Group(
        name=GROUP, description="Query this server's archive from chat")

    @tree.error
    async def _on_error(interaction: discord.Interaction,
                        error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(
                "You need **Manage Server** or **Administrator** to use that.",
                ephemeral=True)
        else:
            log.exception("slash command error")
            await interaction.response.send_message(
                "Something went wrong running that command.", ephemeral=True)

    @group.command(name="help", description="List the archive commands")
    async def help_cmd(interaction: discord.Interaction) -> None:
        e = _embed("Archive commands")
        e.description = (
            "These read the local archive of this server.\n\n"
            "`/archive stats` - message / channel / member / attachment counts\n"
            "`/archive channels` - channels grouped by category, with counts\n"
            "`/archive members [query]` - search the member roster\n"
            "`/archive roles` - list roles\n"
            "`/archive audit [limit]` - recent recorded server changes\n"
            "`/archive search <query> [limit]` - search archived messages\n\n"
            "Data commands require **Manage Server** or **Administrator**.")
        await interaction.response.send_message(embed=e)

    @group.command(name="stats", description="Archive statistics")
    @mod_only
    async def stats(interaction: discord.Interaction) -> None:
        m = db.conn.execute(
            "SELECT COUNT(*) n, SUM(deleted) d FROM messages").fetchone()
        a = db.conn.execute(
            "SELECT COUNT(*) n, SUM(CASE WHEN download_status='done' THEN 1 "
            "ELSE 0 END) done FROM attachments").fetchone()
        ch = db.conn.execute("SELECT COUNT(*) n FROM channels").fetchone()
        mem = db.conn.execute("SELECT COUNT(*) n FROM members").fetchone()
        e = _embed("Archive stats")
        e.add_field(name="Messages", value=f"{m['n'] or 0:,}")
        e.add_field(name="Deleted", value=f"{m['d'] or 0:,}")
        e.add_field(name="Channels", value=f"{ch['n'] or 0:,}")
        e.add_field(name="Members", value=f"{mem['n'] or 0:,}")
        e.add_field(name="Attachments", value=f"{a['done'] or 0:,}/{a['n'] or 0:,}")
        await interaction.response.send_message(embed=e)

    @group.command(name="channels", description="Channels grouped by category")
    @mod_only
    async def channels(interaction: discord.Interaction) -> None:
        rows = db.conn.execute(
            "SELECT c.id, c.name, c.type, c.position, c.category_id, c.parent_id, "
            "(SELECT COUNT(*) FROM messages m WHERE m.channel_id=c.id) n "
            "FROM channels c ORDER BY c.position, c.id").fetchall()
        cats = [r for r in rows if r["type"] == 4]
        chans = [r for r in rows if r["type"] != 4 and r["type"] not in THREAD_TYPES]
        threads = [r for r in rows if r["type"] in THREAD_TYPES]
        by_cat: dict = {}
        for c in chans:
            by_cat.setdefault(c["category_id"] or "", []).append(c)
        thr_by: dict = {}
        for t in threads:
            thr_by.setdefault(t["parent_id"] or "", []).append(t)

        lines: list = []
        def add_chan(c):
            lines.append(f"  #{c['name'] or c['id']} ({c['n']})")
            for t in thr_by.get(c["id"], []):
                lines.append(f"      ↳ {t['name']} ({t['n']})")
        for cat in cats:
            lines.append(f"**{cat['name']}**")
            for c in by_cat.get(cat["id"], []):
                add_chan(c)
        if by_cat.get(""):
            lines.append("**No category**")
            for c in by_cat[""]:
                add_chan(c)

        text = "\n".join(lines)
        if len(text) > _EMBED_LIMIT:
            text = text[:_EMBED_LIMIT] + "\n…(truncated)"
        e = _embed("Channels")
        e.description = text or "none"
        await interaction.response.send_message(embed=e)

    @group.command(name="members", description="Search the member roster")
    @app_commands.describe(query="filter by name (optional)")
    @mod_only
    async def members(interaction: discord.Interaction,
                      query: str | None = None) -> None:
        if query:
            rows = db.conn.execute(
                "SELECT name, display_name, is_bot FROM members "
                "WHERE name LIKE ? OR display_name LIKE ? "
                "ORDER BY name LIMIT 25", (f"%{query}%", f"%{query}%")).fetchall()
        else:
            rows = db.conn.execute(
                "SELECT name, display_name, is_bot FROM members "
                "ORDER BY name LIMIT 25").fetchall()
        total = db.conn.execute("SELECT COUNT(*) n FROM members").fetchone()["n"]
        e = _embed(f"Members ({total:,} total, showing {len(rows)})")
        e.description = "\n".join(
            f"{'🤖 ' if r['is_bot'] else ''}{r['display_name'] or r['name']} "
            f"(@{r['name']})" for r in rows) or "no matches"
        await interaction.response.send_message(embed=e)

    @group.command(name="roles", description="List roles")
    @mod_only
    async def roles(interaction: discord.Interaction) -> None:
        rows = db.conn.execute(
            "SELECT name, colour, position FROM roles "
            "ORDER BY position DESC, name").fetchall()
        e = _embed(f"Roles ({len(rows)})")
        e.description = "\n".join(
            f"`{r['position'] or 0:>2}` {r['name'] or 'unnamed'}"
            for r in rows) or "none"
        await interaction.response.send_message(embed=e)

    @group.command(name="audit", description="Recent recorded server changes")
    @app_commands.describe(limit="how many events (default 10)")
    @mod_only
    async def audit(interaction: discord.Interaction,
                    limit: int = 10) -> None:
        limit = max(1, min(int(limit), 25))
        rows = db.conn.execute(
            "SELECT event, target_name, actor_name, captured_at FROM audit_events "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        lines = []
        for r in rows:
            when = (r["captured_at"] or "").replace("T", " ")[:16]
            actor = f" by {r['actor_name']}" if r["actor_name"] else ""
            lines.append(f"**{r['event']}** {r['target_name'] or ''}{actor} · {when}")
        e = _embed("Recent changes")
        e.description = "\n".join(lines) or "nothing recorded yet"
        await interaction.response.send_message(embed=e)

    @group.command(name="search", description="Search archived messages")
    @app_commands.describe(query="text to search for", limit="max results")
    @mod_only
    async def search(interaction: discord.Interaction, query: str,
                     limit: int = 5) -> None:
        limit = max(1, min(int(limit), 10))
        rows = db.conn.execute(
            "SELECT m.author_name, m.content, m.timestamp, c.name cname "
            "FROM messages m LEFT JOIN channels c ON c.id=m.channel_id "
            "WHERE m.content LIKE ? ORDER BY m.id DESC LIMIT ?",
            (f"%{query}%", limit)).fetchall()
        e = _embed(f"Search: {query}")
        if not rows:
            e.description = "no matches"
        else:
            for r in rows:
                e.add_field(
                    name=f"#{r['cname'] or '?'} · {r['author_name']}",
                    value=_trunc(r["content"]) or "(no text)", inline=False)
        await interaction.response.send_message(embed=e)

    tree.add_command(group)
    # Keep a handle so sync() can bind this group to the target guild. A guild
    # sync only uploads commands registered for that guild; a globally-added
    # command would produce a successful-but-empty sync and never appear.
    client.archive_group = group


async def sync(client: discord.Client) -> None:
    """Register the commands with Discord for the target guild. Call on_ready."""
    guild = (client.get_guild(client.target_guild_id)
             if client.target_guild_id else
             (client.guilds[0] if client.guilds else None))
    if guild is None:
        log.warning("no guild available to sync slash commands")
        return
    try:
        grp = getattr(client, "archive_group", None)
        if grp is not None:
            client.tree.add_command(grp, guild=guild, override=True)
        await client.tree.sync(guild=guild)
        log.info("synced /archive slash commands for %s", guild.name)
    except Exception:
        log.exception("failed to sync slash commands")
