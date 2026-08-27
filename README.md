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

Read-only (archiving only) — `permissions=66560`, which is exactly
`VIEW_CHANNEL | READ_MESSAGE_HISTORY`:

```
https://discord.com/api/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot&permissions=66560
```

With mirroring enabled — `permissions=536988672`, adding
`SEND_MESSAGES | EMBED_LINKS | ATTACH_FILES | MANAGE_WEBHOOKS`:

```
https://discord.com/api/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot&permissions=536988672
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
| `python main.py dashboard` | no | Serve the local dashboard. |

Typical first run:

```bash
python main.py backfill     # history + attachments, checkpointed
python main.py info         # server-info.json, members.csv, channels.csv
python main.py dashboard    # http://localhost:8080
```

Then `python main.py listen` whenever you want live capture running. Ctrl-C
stops it cleanly and flushes any pending attachment downloads.

`backfill` handles SIGINT as a pause: kill it mid-run and restart, and it resumes
from the last checkpoint rather than re-reading the channel.

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
- **Forum threads and threads generally** need per-thread history walks. The
  backfiller covers text, announcement, and forum *channels*; thread archives
  inside them are not walked yet.
- **Very large servers.** Member chunking and history pagination are
  rate-limited by Discord. A server with millions of messages will take hours.
  That's inherent, not a bug.

## Privacy

- `data/` and `.env` are git-ignored. The database contains other people's
  messages and their files — treat it like the sensitive data it is.
- Don't publish an export without telling the people in it.
- Give members an opt-out channel, or exclude sensitive channels by removing
  the bot's View Channel permission on them.

## Tests

```bash
python -m unittest discover -s tests -t . -v
```

59 tests covering the storage layer (including dedupe, edit/delete logging,
resumable cursors, foreign-key enforcement), the export layer (XSS escaping,
format consistency, schema integrity), config loading, and the listener's scope
and intent logic.

**What is not covered:** the gateway connection itself and the CDN download
path, both of which need live credentials and network. `capture.py` is exercised
through stubs that mimic the discord.py objects it reads.
