"""Command-line entry point.

    python main.py backfill    # walk history (resumable)
    python main.py listen      # live capture until you Ctrl-C
    python main.py attachments # download pending images/files
    python main.py export      # write JSON + CSV + HTML
    python main.py info        # write the server-info report
    python main.py stats       # print archive counts
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

import discord

from archiver import attachments as att_mod
from archiver import export as export_mod
from archiver.backfill import backfill_guild
from archiver.config import load as load_config
from archiver.db import Database
from archiver.listener import make_client
from archiver.mirror import Mirror

log = logging.getLogger("archiver")


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("discord").setLevel(logging.WARNING)


# --------------------------------------------------------------- connected


async def _resolve_guild(client: discord.Client, guild_id: int) -> discord.Guild:
    guild = client.get_guild(guild_id) or await client.fetch_guild(guild_id)
    if guild is None:
        raise SystemExit(
            f"Guild {guild_id} is not accessible. Is the bot still a member?"
        )
    return guild


async def run_command(cfg, body) -> None:
    """Shared connect/disconnect harness for the one-shot subcommands."""
    db = Database(cfg.db_path)
    client = make_client(db, guild_id=cfg.guild_id,
                         capture_edits=cfg.capture_edits,
                         capture_deletes=cfg.capture_deletes)

    async def runner():
        await client.wait_until_ready()
        try:
            await body(client, db)
        finally:
            await client.close()

    task = asyncio.create_task(runner())
    await client.start(cfg.bot_token)
    await task


async def _backfill_body(client: discord.Client, db: Database, cfg) -> None:
    guild = await _resolve_guild(client, cfg.guild_id)

    # The member cache is only fully populated for small guilds; chunk it so the
    # roster in the archive is complete rather than partial.
    try:
        await guild.chunk()
    except Exception:
        log.warning("could not chunk member list; roster may be incomplete")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # Windows

    log.info("starting backfill of %s (Ctrl-C to pause; it resumes)", guild.name)
    result = await backfill_guild(db, guild, batch=cfg.backfill_batch, stop=stop)

    for r in result["results"]:
        if r.get("skipped"):
            log.info("  %-28s already complete", r["channel"])
        else:
            log.info("  %-28s %6d new / %6d seen", r.get("name", r["channel"]),
                     r["new"], r["seen"])

    if cfg.download_attachments:
        log.info("downloading attachments...")
        stats = await att_mod.process_pending(
            db, cfg.attachments_dir, max_concurrent=cfg.max_concurrent_downloads)
        log.info("attachments: %s", stats)

    log.info("archive totals: %s", db.stats(str(cfg.guild_id)))


async def _listen_body(client: discord.Client, db: Database, cfg) -> None:
    guild = await _resolve_guild(client, cfg.guild_id)
    mirror = None
    if cfg.mirror_enabled and cfg.mirror_guild_id:
        mirror = Mirror(client, source_guild_id=cfg.guild_id,
                        target_guild_id=cfg.mirror_guild_id)
        # Wire the mirror into the client so on_message forwards.
        client.mirror = mirror
        log.info("mirroring %s -> guild %s", guild.name, cfg.mirror_guild_id)

    log.info("live capture running on %s. Ctrl-C to stop.", guild.name)
    log.info("archive so far: %s", db.stats(str(cfg.guild_id)))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    await stop.wait()

    if cfg.download_attachments:
        log.info("flushing pending attachment downloads...")
        stats = await att_mod.process_pending(
            db, cfg.attachments_dir, max_concurrent=cfg.max_concurrent_downloads)
        log.info("attachments: %s", stats)
    log.info("session counters: %s", client.counters)
    log.info("archive totals: %s", db.stats(str(cfg.guild_id)))


# ----------------------------------------------------------------- offline


def cmd_export(cfg, args) -> None:
    db = Database(cfg.db_path)
    out = Path(args.out) if args.out else cfg.exports_dir / "latest"
    paths = export_mod.export_all(
        db, out,
        guild_id=str(cfg.guild_id) if cfg.guild_id else None,
        channel_id=args.channel,
        include_deleted=not args.exclude_deleted,
        title=args.title,
    )
    for kind, p in paths.items():
        print(f"  {kind:5s} -> {p}")
    print("totals:", db.stats(str(cfg.guild_id) if cfg.guild_id else None))
    db.close()


def cmd_info(cfg, args) -> None:
    if not cfg.guild_id:
        raise SystemExit("DISCORD_GUILD_ID must be set to produce a server-info report")
    db = Database(cfg.db_path)
    out = Path(args.out) if args.out else cfg.exports_dir / "server-info"
    paths = export_mod.export_server_info(db, str(cfg.guild_id), out)
    for kind, p in paths.items():
        print(f"  {kind:12s} -> {p}")
    db.close()


def cmd_stats(cfg, args) -> None:
    db = Database(cfg.db_path)
    stats = db.stats(str(cfg.guild_id) if cfg.guild_id else None)
    for k, v in stats.items():
        print(f"  {k:26s} {v}")
    db.close()


def cmd_attachments(cfg, args) -> None:
    db = Database(cfg.db_path)
    pending = db.pending_attachments(limit=10_000)
    print(f"{len(pending)} pending attachment(s)")
    stats = asyncio.run(att_mod.process_pending(
        db, cfg.attachments_dir, max_concurrent=cfg.max_concurrent_downloads))
    print("result:", stats)
    db.close()


def cmd_dashboard(cfg, args) -> None:
    from dashboard.app import create_app
    app = create_app(cfg.db_path)
    print(f"dashboard on http://0.0.0.0:{args.port}")
    app.run(host="0.0.0.0", port=args.port, debug=args.debug)


# -------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="archiver", description="Discord server archiver")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--env", default=".env")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("backfill", help="walk channel history (resumable)")

    sub.add_parser("listen", help="live-capture new messages until interrupted")

    e = sub.add_parser("export", help="write JSON + CSV + HTML")
    e.add_argument("--out", help="output directory")
    e.add_argument("--channel", help="restrict to one channel id")
    e.add_argument("--exclude-deleted", action="store_true")
    e.add_argument("--title", help="title for the HTML export")

    i = sub.add_parser("info", help="write the server-info report")
    i.add_argument("--out", help="output directory")

    sub.add_parser("stats", help="print archive counts")

    a = sub.add_parser("attachments", help="download pending attachments")
    a.set_defaults(func=cmd_attachments)

    d = sub.add_parser("dashboard", help="serve the local dashboard")
    d.add_argument("--port", type=int, default=8080)
    d.add_argument("--debug", action="store_true")

    return p


# Only some commands touch the network or a specific guild. Requiring a token
# for `stats` would be wrong: it reads the local database and nothing else.
NEEDS_TOKEN = {"backfill", "listen"}
NEEDS_GUILD = {"backfill", "listen", "info"}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    cfg = load_config(args.env)

    problems = cfg.validate(need_token=args.command in NEEDS_TOKEN,
                            need_guild=args.command in NEEDS_GUILD)
    if problems:
        for p in problems:
            print(f"error: {p}", file=sys.stderr)
        return 2

    if args.command == "backfill":
        asyncio.run(run_command(cfg, lambda c, db: _backfill_body(c, db, cfg)))
    elif args.command == "listen":
        asyncio.run(run_command(cfg, lambda c, db: _listen_body(c, db, cfg)))
    elif args.command == "export":
        cmd_export(cfg, args)
    elif args.command == "info":
        cmd_info(cfg, args)
    elif args.command == "stats":
        cmd_stats(cfg, args)
    elif args.command == "attachments":
        cmd_attachments(cfg, args)
    elif args.command == "dashboard":
        cmd_dashboard(cfg, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
