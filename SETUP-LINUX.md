# Setup guide — Linux, from nothing

Written for someone who has never run this before. Every command is followed by
what it does and what you should see back. If what you see doesn't match, stop
and check the troubleshooting section at the bottom rather than pushing on.

**Time: about 15 minutes.** You need a terminal (usually `Ctrl+Alt+T`) and your
Discord bot token.

---

## Step 0 — Find out which Linux you're on

```bash
cat /etc/os-release
```

Look at the `ID=` line. `ubuntu` and `debian` use `apt`. `fedora` uses `dnf`.
`arch` uses `pacman`. That decides the next command.

---

## Step 1 — Install Python and git

**Ubuntu / Debian:**

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git
```

> `python3-venv` is a separate package on Debian/Ubuntu and people forget it.
> Without it, Step 3 fails with `ensurepip is not available`.

**Fedora:**

```bash
sudo dnf install -y python3 python3-pip git
```

**Arch:**

```bash
sudo pacman -S --needed python python-pip git
```

Now check the version:

```bash
python3 --version
```

**You should see `Python 3.10` or higher.** 3.11, 3.12, 3.13 are all fine.

If you see 3.8 or 3.9, your distro is too old for this — you're probably on
Ubuntu 20.04. Stop here and either upgrade the OS or install a newer Python
from deadsnakes (Ubuntu) before continuing.

---

## Step 2 — Download the code

```bash
cd ~
git clone https://github.com/wuyiomokunrin-cell/discord-archiver.git
cd discord-archiver
```

**You should see** git downloading files, then a prompt that now ends in
`discord-archiver`.

This puts the code in `/home/yourname/discord-archiver`. That's the folder
everything else happens in.

*No git and don't want to install it?* On GitHub, green **Code** button →
**Download ZIP**, extract it, and `cd` into the extracted folder instead.

---

## Step 3 — Create the isolated environment and install libraries

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

What these do, in order:

1. Makes a private folder called `.venv` holding its own Python. Nothing you
   install here can break the rest of your system.
2. Switches your terminal into it. **Your prompt gains a `(.venv)` prefix** —
   that prefix is how you know it worked.
3. Installs `discord.py` and `flask` into that folder.

**You should see** a wall of download progress, ending in
`Successfully installed ...`.

> **This is the step people forget later.** Every time you open a new terminal
> you have to run `source .venv/bin/activate` again. If your prompt doesn't
> show `(.venv)`, the commands below won't find the libraries.

---

## Step 4 — Put your token and server ID in `.env`

```bash
cp .env.example .env
nano .env
```

You'll see the file's contents. Fill in two lines:

```
DISCORD_BOT_TOKEN=paste-your-token-here
DISCORD_GUILD_ID=123456789012345678
```

**Getting the token:** <https://discord.com/developers/applications> → your app
→ **Bot** → **Reset Token** → copy. It starts with a long random string.

**Getting the server ID:** in Discord, **User Settings → Advanced → Developer
Mode → ON**. Then right-click your server's icon in the left sidebar →
**Copy Server ID**. It's a long number.

Save and exit nano: **Ctrl+O**, then **Enter** to confirm, then **Ctrl+X**.

> `.env` is in `.gitignore`, so it can never be accidentally committed. Still —
> that token is a password. Don't paste it into a chat, a screenshot, or
> anywhere public. If it leaks, Reset Token in the Portal kills the old one.

---

## Step 5 — Check it's wired up

```bash
python main.py --help
```

**You should see** a list of commands: `backfill, listen, export, info, stats,
attachments, dashboard`. That means Python found the code.

```bash
python main.py stats
```

**You should see** a table of zeros — the archive is empty, that's expected.
The point is that it *runs* without an error.

> If this says `No module named 'discord'`, your venv isn't active. Run
> `source .venv/bin/activate` and try again.

---

## Step 6 — Turn on the two privileged intents

Before the next step will work, in the Developer Portal → your app → **Bot** →
**Privileged Gateway Intents**, enable:

- **MESSAGE CONTENT INTENT** ← required. Without it the bot connects fine and
  silently records every message as an empty string.
- **SERVER MEMBERS INTENT** ← required for the member list.

Leave **Presence Intent** off; this project doesn't use it.

Skip this and you'll get `Gateway error 4014: Disallowed intent(s)` in Step 7.

---

## Step 7 — Archive your server

```bash
python main.py backfill
```

**You should see** it connecting, then lines like:

```
12:04:31 INFO  archiver.backfill: catalogued My Server: 14 channels, 6 roles, 203 members
12:04:33 INFO  archiver.backfill: backfilling 9 channels in My Server
12:04:35 INFO  archiver.backfill: #general: 100 messages (100 new)
```

Leave it running. A small server takes seconds; a server with years of history
can take an hour or more, because Discord rate-limits how fast anyone can read
history. That's normal, not a hang.

**Ctrl+C stops it safely.** It checkpoints as it goes, so restarting picks up
where it left off instead of starting over.

When it finishes:

```bash
python main.py stats        # confirm message count is not zero
python main.py info         # writes the server-info report
python main.py dashboard    # then open http://localhost:8080
```

---

## Step 8 — Live capture, whenever you want it

```bash
cd ~/discord-archiver
source .venv/bin/activate
python main.py listen
```

The bot goes online and records new messages until you press **Ctrl+C** or close
the terminal. That's the "online only when I'm online" behaviour — there's
nothing else to configure.

**Keep the terminal open.** Closing the window kills the bot.

---

## Stopping, restarting, and where things are

| To | Do |
|---|---|
| Stop the bot | `Ctrl+C` in its terminal |
| Start it again later | `cd ~/discord-archiver && source .venv/bin/activate && python main.py listen` |
| Find the archive | `~/discord-archiver/data/archive.sqlite3` |
| Find downloaded images | `~/discord-archiver/data/attachments/` |
| Find exports | `~/discord-archiver/data/exports/` |
| Remove everything | delete the `discord-archiver` folder |

The `data/` folder holds other people's messages and files. Treat it
accordingly, and note that it's git-ignored so it won't be pushed anywhere.

---

## Troubleshooting

**`python: command not found`**
Use `python3` instead. Some distros don't symlink `python`. If `python3` also
fails, Step 1 didn't run.

**`No module named 'discord'`**
The venv isn't active. `source .venv/bin/activate`, then retry. Check for
`(.venv)` in your prompt.

**`ensurepip is not available`** when creating the venv
On Debian/Ubuntu: `sudo apt install -y python3-venv`, then redo Step 3.

**`Gateway error 4014: Disallowed intent(s)`**
Step 6. The intents aren't enabled in the Portal.

**`Privileged message content intent is not enabled`**
Same fix — but note the bot will still *run*, it just records empty messages.

**`LoginFailure: Improper token`**
The token in `.env` is wrong or has extra spaces. Reset Token in the Portal and
paste it again.

**`Guild ... is not accessible`**
The bot isn't in your server. Re-invite it with the link from the README
(`permissions=66560`).

**Archive has messages but every `content` is empty**
MESSAGE CONTENT INTENT. This is the single most common failure, and it fails
silently, which is why it's listed three times in this document.

**It's very slow**
Discord rate-limits history reads for everyone. Let it run; it checkpoints, so
you can Ctrl+C and resume.

**Anything else**
Add `-v` for detail: `python main.py -v backfill`. Copy the last few lines of
output — that's what's needed to diagnose it.
