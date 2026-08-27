# discord-archiver

Archives a Discord server you own: messages, edits, deletions, reactions,
attachments, members, channels, and roles. Exports to JSON, CSV, and HTML, and
serves a local dashboard for browsing and searching.

Runs as a normal bot on your own machine — so it uses your IP and only runs when
you start it. No self-botting, no user-account automation.

## The one rule

**Only run this against a server you own or administer.**

A Discord server is full of other people's messages. Capturing them is a real
act of data collection, and if you're in the EU, GDPR applies to it. Tell your
members it's running. Discord's Developer Terms §2.4 also restricts collecting
and disclosing end-user data, and self-bots (automating a user account with a
user token) are bannable outright — this project deliberately doesn't do that.

## Setup

### 1. Create the application

1. Go to <https://discord.com/developers/applications> → **New Application**
2. **Bot** tab → **Reset Token** → copy it
3. Still on the **Bot** tab, under **Privileged Gateway Intents**, enable:
   - **MESSAGE CONTENT INTENT** — required. Without it `message.content` arrives
     as an empty string and the archive silently fills with blanks.
   - **SERVER MEMBERS INTENT** — required for a complete member roster
   - Presence Intent — *not* needed, leave it off

> Under 10,000 users you can toggle these yourself in the Portal. Above that you
> must apply for access, and re-authorise yearly. A personal bot on one server
> is nowhere near that line.

### 2. Invite it

Recommended (archiving + ping + audit log) — `permissions=68736`, which is
`VIEW_CHANNEL | READ_MESSAGE_HISTORY | SEND_MESSAGES | VIEW_AUDIT_LOG`:

```
https://discord.com/api/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot&permissions=68736
```

- `SEND_MESSAGES` is only needed for the `!ping` latency reply.
- `VIEW_AUDIT_LOG` is only needed to pull the offline audit log; live
  change events work without it.

Minimal (archiving only) — `permissions=66560` (`VIEW_CHANNEL | READ_MESSAGE_HISTORY`):

```
https://discord.com/api/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot&permissions=66560
```

With mirroring enabled — `permissions=536988800`, adding
`EMBED_LINKS | ATTACH_FILES | MANAGE_WEBHOOKS`:

```
https://discord.com/api/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot&permissions=536988800
```

**Do not grant Administrator.** The bot needs to read, not to manage.

### 3. Configure

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # then fill in DISCORD_BOT_TOKEN and DISCORD_GUILD_ID
```

## Commands

| Command | Needs token | What it does |
|---|---|---|
| `python main.py backfill` | yes | Walk every channel's history. Resumable. |
| `python main.py listen` | yes | Live-capture until you Ctrl-C. |
| `python main.py attachments` | no | Download pending images and files. |
| `python main.py export` | no | Write JSON + CSV + HTML. |
| `python main.py info` | no | Write the server-info report. |
| `python main.py stats` | no | Print archive counts. |
| `python main.py progress` | no | Per-channel backfill status. |
| `python main.py audit` | no | Recent recorded server changes. |
| `python main.py dashboard` | no | Serve the local dashboard. |

Typical first run:

```bash
python main.py backfill     # history + attachments, checkpointed
python main.py info         # server-info.json, members.csv, channels.csv
python main.py dashboard    # http://localhost:8080
```

Then `python main.py listen` whenever you want live capture running. Ctrl-C
stops it cleanly and flushes any pending attachment downloads.

Only one archiver process may run at a time; a second one exits immediately with
a message rather than corrupting the database. Find a stray one with
`pgrep -af "main.py"`.

`backfill` handles SIGINT as a pause: kill it mid-run and restart, and it resumes
from the last checkpoint rather than re-reading the channel. It prints an
explicit `BACKFILL COMPLETE` or `BACKFILL PAUSED` block at the end so you are
not left guessing whether it finished.

`python main.py progress` answers "is it done yet?" from another terminal while
a backfill is running:

```
$ python main.py progress
2 of 9 channels fully backfilled

  CHANNEL                           MESSAGES  OLDEST               STATUS
  ------------------------------------------------------------------------------
  #general                             18420  2023-04-02 11:16:00  done
  #media                                3102  2023-05-19 09:02:00  done
  #off-topic                               0  -                    PENDING

  -> 7 channel(s) still pending. Re-run `python main.py backfill` to resume.
```

## How it works

```
archiver/
  db.py          SQLite schema + access layer
  capture.py     shared normalisation; live and backfill paths funnel through here
  backfill.py    resumable history walk, cursor in sync_state
  listener.py    gateway events: message, edit, delete, reaction, member leave
  attachments.py CDN download, SHA-256 content addressing, dedupe
  export.py      schema-driven JSON / CSV / HTML + server-info report
  mirror.py      optional webhook mirroring into a second server
  audit.py       server-change capture: live events + offline audit-log pull
  lock.py        cross-platform single-instance lock (POSIX + Windows)
dashboard/       Flask app, read-only, no Discord calls
```

**Capture is idempotent.** History pagination and live events will hand you the
same message twice; `insert_message` uses `INSERT OR IGNORE` and reports whether
the row was new. Attachments are recorded even on a duplicate capture, so a
re-run is self-healing.

**Attachments are downloaded, not linked.** Discord CDN URLs are signed and
expire, so storing the URL produces an archive that rots. Files are
content-addressed by SHA-256 and sharded two hex chars deep, so identical files
posted twice cost one copy on disk.

**Deleted messages keep their content.** Deletion sets a flag and logs to
`message_deletes`; the text stays, because losing it defeats the purpose.

**Mirroring uses webhooks**, so mirrored messages show the original author's
name and avatar. The bot's own messages are never captured or mirrored, so
content can't loop between the two servers.

### Schema

`guilds`, `channels`, `roles`, `members`, `member_roles`, `messages`,
`attachments`, `message_edits`, `message_deletes`, `reactions`, `sync_state`,
`meta`.

Snowflake IDs are `TEXT`, not integers — a 19-digit ID is beyond JavaScript's
`MAX_SAFE_INTEGER`, and JSON exports consumed by a browser would silently lose
precision.

### The export template

`EXPORT_SCHEMA` in `export.py` is the single source of truth for what an
exported message contains. Each field declares whether it appears in JSON, CSV,
or HTML. Add one entry and all three formats pick it up; a test fails if a
`source` key doesn't exist in the flattened message, so typos can't silently
emit nulls.

## Limitations

Worth knowing before you rely on the output:

- **No backfill before the bot joined.** `READ_MESSAGE_HISTORY` reaches the
  channel's existing history, but the bot cannot see channels it has no
  permission for, and cannot see anything in channels created after it leaves.
- **No DMs between other users.** Not accessible to bots, by design.
- **No voice audio.** Capturing voice is a separate problem requiring a voice
  gateway connection and recording; not implemented.
- **Edits and deletes are only caught while listening.** A message edited and
  deleted while the process was down looks like it never changed. Backfill sees
  the current state only.
- **Threads inside a text channel** are not walked. Forum threads are (a
  `ForumChannel` has no `history()` of its own, so the backfiller enumerates
  its active and archived threads and walks each). Threads opened in an
  ordinary text channel still are not.
- **Very large servers.** Member chunking and history pagination are
  rate-limited by Discord. A server with millions of messages will take hours.
  That's inherent, not a bug.

## Live features while listening

`python main.py listen` also:

- **Answers `!ping`** (or a mention with "ping") with the gateway latency in ms.
  Needs `SEND_MESSAGES`.
- **Records edits and deletions** of archived messages (already on by default).
- **Captures server changes as they happen**: channel/role create-update-delete,
  member join/leave/ban/unban, nickname and role changes, guild renames. See
  them with `python main.py audit` or `/api/audit` on the dashboard.
- **Pulls the audit log on startup** to catch changes that happened while the
  bot was offline. Needs `VIEW_AUDIT_LOG`; skipped gracefully without it.

## Running on Windows (dual boot)

The code is cross-platform. The only differences from Linux/macOS:

```
python -m venv .venv
.venv\Scripts\activate        <- note: Scripts, not bin
pip install -r requirements.txt
```

Everything else (`python main.py backfill` etc.) is identical. The run lock
uses `msvcrt.locking` on Windows instead of `fcntl.flock`, so two instances are
still prevented. Clone the same repo on both OSes; they share nothing except
your Discord credentials, and each keeps its own `data/` folder - so a Windows
run starts a fresh archive unless you copy `data/` across.

## Run it without a terminal (autostart)

`scripts/run.sh` (Linux/macOS) and `scripts\run.bat` (Windows) do the whole
setup-and-run dance: create the venv if missing, install dependencies if
missing, backfill **only if there is no archive yet**, then live-listen. So a
reboot never requires retyping anything.

To make it start automatically:

- **Linux:** `scripts/install-autostart.sh` installs a per-user systemd service
  with linger, so it starts at boot and restarts on failure. Control it with
  `systemctl --user status/stop discord-archiver` and
  `journalctl --user -u discord-archiver -f`. Remove with
  `scripts/uninstall-autostart.sh`.
- **Windows:** `scripts\install-autostart.bat` creates a Task Scheduler task
  that runs the launcher at logon. Remove with `scripts\uninstall-autostart.bat`.

Because `run.*` is idempotent and the run lock refuses a second instance, it is
safe to let it fire on every boot and also start one by hand - the second one
just exits with a message.

## Privacy

- `data/` and `.env` are git-ignored. The database contains other people's
  messages and their files — treat it like the sensitive data it is.
- Don't publish an export without telling the people in it.
- Give members an opt-out channel, or exclude sensitive channels by removing
  the bot's View Channel permission on them.

## Slash commands (control from chat)

While `listen` is running, the bot registers a `/archive` command group in your
server so other admins can query the archive without anyone opening a terminal:

- `/archive help` - list the commands (available to everyone)
- `/archive stats` - message / channel / member / attachment counts
- `/archive channels` - channels grouped by category, Discord-style
- `/archive members [query]` - search the member roster
- `/archive roles` - list roles
- `/archive audit [limit]` - recent recorded server changes
- `/archive search <query> [limit]` - search archived messages

Data commands require **Manage Server** or **Administrator**. They read the same
local database the dashboard uses, so other admins see the same data in chat -
the dashboard web page itself still lives on your machine only. Commands are
synced to the guild automatically when the bot connects.

The dashboard sidebar is organised like Discord: categories as headers, channels
beneath them, and threads nested under their parent channel.

## Releases

Tagged releases live at the repo's Releases page; each ships automatic
`Source code (zip/tar.gz)` downloads, so you can install without git:

- <https://github.com/wuyiomokunrin-cell/discord-archiver/releases>

To cut a new one: bump `__version__` in `archiver/__init__.py`, commit, push,
then create a release in the GitHub UI (or API) with a tag like `v1.1.0`
pointing at `main`. Pin downloads to a tag if you want a stable version rather
than the moving `main`.

## Tests

```bash
python -m unittest discover -s tests -t . -v
```

139 tests covering the storage layer (dedupe, edit/delete logging, resumable
cursors, foreign-key enforcement), cold-start capture against an empty
database, the export layer (XSS escaping, format consistency, schema
integrity), config loading, listener scope and intents, and a full
populate -> export -> dashboard integration pass.

Several of those tests exist because production found bugs the suite could not.
`TestCaptureColdStart` runs against a completely empty database, and
`TestCaptureColdStartWithRoles` gives the author roles - the original fixtures
pre-created the guild and used an empty role list, which is exactly why both
foreign-key races went unnoticed.

**What is not covered:** the gateway connection itself and the CDN download
path, both of which need live credentials and network. `capture.py` is exercised
through stubs that mimic the discord.py objects it reads.
