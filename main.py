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
import time
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
    # discord.py warns about missing PyNaCl/davey on every start. This project
    # never touches voice, so those warnings are pure noise to a first-time user.
    logging.getLogger("discord").setLevel(logging.ERROR)


# --------------------------------------------------------------- connected


async def _resolve_guild(client: discord.Client, guild_id: int) -> discord.Guild:
    guild = client.get_guild(guild_id) or await client.fetch_guild(guild_id)
    if guild is None:
        raise SystemExit(
            f"Guild {guild_id} is not accessible. Is the bot still a member?"
        )
    return guild


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _die(headline: str, what_to_do: str) -> None:
    """Print a readable error instead of a traceback for known failure modes."""
    print(f"\nerror: {headline}", file=sys.stderr)
    print(f"  -> {what_to_do}\n", file=sys.stderr)


async def run_command(cfg, body) -> int:
    """Shared connect/disconnect harness for the one-shot subcommands.

    Translates the discord.py exceptions a first-time user is most likely to hit
    into plain instructions. An unhandled LoginFailure otherwise prints a
    traceback plus an aiohttp "Unclosed connector" warning, which reads like a
    crash even though the fix is a typo in .env.
    """
    db = Database(cfg.db_path)
    client = make_client(db, guild_id=cfg.guild_id,
                         capture_edits=cfg.capture_edits,
                         capture_deletes=cfg.capture_deletes)
    runner_task = None

    try:
        async def runner():
            await client.wait_until_ready()
            try:
                await body(client, db)
            finally:
                await client.close()

        runner_task = asyncio.create_task(runner())
        await client.start(cfg.bot_token)
        await runner_task
        return 0

    except discord.LoginFailure:
        _die("Discord rejected the bot token.",
             "Open .env and check DISCORD_BOT_TOKEN. It must be the exact string "
             "from the Developer Portal (Bot tab -> Reset Token), with no spaces, "
             "quotes, or line breaks. If you reset the token, the old one is dead.")
        return 3

    except discord.PrivilegedIntentsRequired:
        _die("Discord refused the connection: a privileged intent is switched off.",
             "Developer Portal -> your app -> Bot -> Privileged Gateway Intents. "
             "Enable MESSAGE CONTENT INTENT and SERVER MEMBERS INTENT, click Save, "
             "then run this again.")
        return 4

    except discord.GatewayNotFound:
        _die("Could not reach Discord's gateway.",
             "Discord may be having an outage. Wait a minute and try again.")
        return 5

    except discord.HTTPException as exc:
        _die(f"Discord returned an HTTP error: {exc}",
             "Check your internet connection, and that the bot is still a member "
             "of the server.")
        return 6

    finally:
        if runner_task is not None and not runner_task.done():
            runner_task.cancel()
        # Closing the client releases the aiohttp connector; skipping this is
        # what produces the misleading "Unclosed connector" warning.
        try:
            await client.close()
        except Exception:
            pass
        db.close()


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

    started = time.monotonic()
    log.info("starting backfill of %s (Ctrl-C to pause; it resumes)", guild.name)
    result = await backfill_guild(db, guild, batch=cfg.backfill_batch, stop=stop)
    elapsed = time.monotonic() - started

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

    remaining = db.channels_needing_backfill(str(cfg.guild_id))
    totals = db.stats(str(cfg.guild_id))

    # An explicit terminal line. Without this the run just ends on a dict of
    # counts and it is not obvious whether it finished or stopped early.
    print()
    if stop.is_set() or remaining:
        print("=" * 62)
        print("  BACKFILL PAUSED - not finished")
        print(f"  {len(remaining)} channel(s) still to do: "
              + ", ".join("#" + (c["name"] or c["id"]) for c in remaining[:8])
              + (" ..." if len(remaining) > 8 else ""))
        print("  Re-run `python main.py backfill` to resume from the checkpoint.")
        print("=" * 62)
    else:
        print("=" * 62)
        print("  BACKFILL COMPLETE")
        print(f"  {totals['messages']} messages across "
              f"{len(result['results'])} channels")
        print(f"  {totals['attachments_downloaded']}/{totals['attachments']} "
              f"attachments downloaded")
        print(f"  finished in {_fmt_duration(elapsed)}")
        print("  Run `python main.py dashboard` to browse it.")
        print("=" * 62)
    print()
    log.info("archive totals: %s", totals)


async def _listen_body(client: discord.Client, db: Database, cfg) -> None:
    guild = await _resolve_guild(client, cfg.guild_id)

    # Catalogue on startup so `listen` works on a fresh database and so the
    # roster and role list are complete rather than filled in message by message.
    try:
        await guild.chunk()
    except Exception:
        log.warning("could not chunk member list; roster may be incomplete")
    try:
        from archiver.backfill import catalog_guild
        await catalog_guild(db, guild)
    except Exception:
        log.exception("catalogue failed; continuing with on-demand capture")

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


def cmd_progress(cfg, args) -> None:
    """Per-channel backfill status: the answer to 'is it finished yet?'."""
    db = Database(cfg.db_path)
    rows = db.backfill_progress(str(cfg.guild_id) if cfg.guild_id else None)
    if not rows:
        print("No channels recorded yet. Run `python main.py backfill` first.")
        db.close()
        return

    done = [r for r in rows if r["complete"]]
    print(f"{len(done)} of {len(rows)} channels fully backfilled\n")
    print(f"  {'CHANNEL':32s} {'MESSAGES':>9s}  {'OLDEST':19s}  STATUS")
    print("  " + "-" * 78)
    for r in rows:
        name = "#" + (r["name"] or r["id"])
        status = "done" if r["complete"] else "PENDING"
        oldest = (r["oldest"] or "-").replace("T", " ")[:19]
        print(f"  {name[:32]:32s} {r['archived']:>9d}  {oldest:19s}  {status}")

    print()
    totals = db.stats(str(cfg.guild_id) if cfg.guild_id else None)
    print(f"  total messages: {totals['messages']}")
    print(f"  attachments:    {totals['attachments_downloaded']}"
          f"/{totals['attachments']} downloaded")
    if len(done) == len(rows):
        print("\n  -> backfill is complete for every channel.")
    else:
        print(f"\n  -> {len(rows) - len(done)} channel(s) still pending."
              " Re-run `python main.py backfill` to resume.")
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

    sub.add_parser("progress", help="show per-channel backfill status")

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
        return asyncio.run(run_command(cfg, lambda c, db: _backfill_body(c, db, cfg)))
    elif args.command == "listen":
        return asyncio.run(run_command(cfg, lambda c, db: _listen_body(c, db, cfg)))
    elif args.command == "export":
        cmd_export(cfg, args)
    elif args.command == "info":
        cmd_info(cfg, args)
    elif args.command == "stats":
        cmd_stats(cfg, args)
    elif args.command == "progress":
        cmd_progress(cfg, args)
    elif args.command == "attachments":
        cmd_attachments(cfg, args)
    elif args.command == "dashboard":
        cmd_dashboard(cfg, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
