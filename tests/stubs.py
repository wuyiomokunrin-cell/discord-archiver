"""Stubs that mimic the parts of the discord.py objects the archiver touches.

These let the capture and export paths be exercised without a live gateway
connection, which is the only part that genuinely needs a bot token.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional


@dataclass
class FakeEnum:
    value: int


@dataclass
class FakeEmbed:
    title: str = ""
    description: str = ""

    def to_dict(self) -> dict:
        return {"title": self.title, "description": self.description}


@dataclass
class FakeAvatar:
    url: str


@dataclass
class FakeAttachment:
    id: str
    filename: str
    url: str
    size: int = 1024
    content_type: Optional[str] = "image/png"
    width: Optional[int] = 800
    height: Optional[int] = 600
    is_spoiler: bool = False


@dataclass
class FakeReference:
    message_id: Optional[int] = None


@dataclass
class FakeRole:
    id: int
    name: str = "member"


@dataclass
class FakeAuthor:
    id: int
    name: str = "someone"
    display_name: str = "Someone"
    bot: bool = False
    roles: list = field(default_factory=list)
    joined_at: Optional[datetime] = None
    display_avatar: Optional[FakeAvatar] = None


@dataclass
class FakeGuild:
    id: int
    name: str = "Test Server"


@dataclass
class FakeChannel:
    id: int
    guild: FakeGuild
    name: str = "general"
    type: Any = None
    position: int = 0
    category: Optional[Any] = None
    topic: Optional[str] = None
    nsfw: bool = False

    def __post_init__(self):
        if self.type is None:
            self.type = FakeEnum(0)


@dataclass
class FakeMessage:
    id: int
    channel: FakeChannel
    author: FakeAuthor
    content: str = "hello"
    created_at: datetime = field(
        default_factory=lambda: datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc))
    edited_at: Optional[datetime] = None
    type: Any = field(default_factory=lambda: FakeEnum(0))
    pinned: bool = False
    reference: Optional[FakeReference] = None
    embeds: list = field(default_factory=list)
    attachments: list = field(default_factory=list)

    @property
    def guild(self) -> FakeGuild:
        return self.channel.guild


def make_message(mid: int, *, channel: FakeChannel | None = None,
                 author: FakeAuthor | None = None, content: str = "hello",
                 minute: int = 0, attachments: list | None = None) -> FakeMessage:
    guild = FakeGuild(id=111, name="Test Server")
    ch = channel or FakeChannel(id=222, guild=guild, name="general")
    au = author or FakeAuthor(id=333, name="alice", display_name="Alice",
                              display_avatar=FakeAvatar(url="https://cdn.example/a.png"))
    return FakeMessage(
        id=mid, channel=ch, author=au, content=content,
        created_at=datetime(2026, 8, 27, 12, minute, tzinfo=timezone.utc),
        attachments=attachments or [],
    )
