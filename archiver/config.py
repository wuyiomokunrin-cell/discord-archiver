"""Configuration, loaded from the environment with an optional .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader. No dependency on python-dotenv."""
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        os.environ.setdefault(key, value)


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    # --- Discord credentials -------------------------------------------------
    bot_token: str = field(default_factory=lambda: os.environ.get("DISCORD_BOT_TOKEN", ""))
    guild_id: int | None = field(
        default_factory=lambda: int(os.environ["DISCORD_GUILD_ID"])
        if os.environ.get("DISCORD_GUILD_ID") else None
    )

    # --- Optional mirroring target ------------------------------------------
    mirror_guild_id: int | None = field(
        default_factory=lambda: int(os.environ["MIRROR_GUILD_ID"])
        if os.environ.get("MIRROR_GUILD_ID") else None
    )

    # --- Storage -------------------------------------------------------------
    data_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("ARCHIVER_DATA_DIR", "data"))
    )
    db_path: Path = field(init=False)
    attachments_dir: Path = field(init=False)
    exports_dir: Path = field(init=False)

    # --- Behaviour -----------------------------------------------------------
    download_attachments: bool = field(
        default_factory=lambda: _bool("DOWNLOAD_ATTACHMENTS", True)
    )
    capture_edits: bool = field(default_factory=lambda: _bool("CAPTURE_EDITS", True))
    capture_deletes: bool = field(default_factory=lambda: _bool("CAPTURE_DELETES", True))
    mirror_enabled: bool = field(default_factory=lambda: _bool("MIRROR_ENABLED", False))
    # Rate-limit safety valve for the history backfill walk.
    backfill_batch: int = field(
        default_factory=lambda: int(os.environ.get("BACKFILL_BATCH", "100"))
    )
    max_concurrent_downloads: int = field(
        default_factory=lambda: int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "5"))
    )

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        self.db_path = self.data_dir / "archive.sqlite3"
        self.attachments_dir = self.data_dir / "attachments"
        self.exports_dir = self.data_dir / "exports"
        for d in (self.data_dir, self.attachments_dir, self.exports_dir):
            d.mkdir(parents=True, exist_ok=True)

    def validate(self, *, need_token: bool = True, need_guild: bool = True) -> list[str]:
        """Return a list of human-readable problems. Empty list means OK."""
        problems: list[str] = []
        if need_token and not self.bot_token:
            problems.append(
                "DISCORD_BOT_TOKEN is not set. Create an application at "
                "https://discord.com/developers/applications and copy the bot token."
            )
        if need_guild and not self.guild_id:
            problems.append(
                "DISCORD_GUILD_ID is not set. Enable Developer Mode in Discord, "
                "right-click your server, and choose Copy Server ID."
            )
        return problems


def load(env_file: str | Path = ".env") -> Config:
    load_dotenv(env_file)
    return Config()
