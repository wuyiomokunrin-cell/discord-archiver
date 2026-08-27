"""Optional mirroring into a second server you control.

Uses webhooks rather than the bot's own send permission. Webhooks let each
mirrored message display the original author's name and avatar, which a plain
bot send cannot do, and they carry a separate rate-limit bucket.

Loop safety: messages authored by the bot itself are never mirrored, and the
mirror target is excluded from capture, so content cannot bounce back and forth.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord

log = logging.getLogger("archiver.mirror")

WEBHOOK_PREFIX = "[mirror] "


class Mirror:
    def __init__(self, client: discord.Client, source_guild_id: int,
                 target_guild_id: int):
        self.client = client
        self.source_guild_id = source_guild_id
        self.target_guild_id = target_guild_id
        # channel id (source) -> webhook
        self._hooks: dict[int, discord.Webhook] = {}
        self.counters = {"mirrored": 0, "failed": 0, "skipped": 0}

    async def _webhook_for(self, source_channel: discord.abc.GuildChannel
                           ) -> Optional[discord.Webhook]:
        if source_channel.id in self._hooks:
            return self._hooks[source_channel.id]

        target_guild = self.client.get_guild(self.target_guild_id)
        if target_guild is None:
            log.error("mirror target guild %s not accessible to the bot", self.target_guild_id)
            return None

        # Find or create a matching channel in the target guild.
        name = getattr(source_channel, "name", "mirror")
        target = discord.utils.get(target_guild.text_channels, name=name)
        if target is None:
            try:
                target = await target_guild.create_text_channel(
                    name, topic=f"Mirrored from {source_channel.guild.name}/#{name}")
            except discord.Forbidden:
                log.error("cannot create channel #%s in mirror target", name)
                return None

        existing = await target.webhooks()
        hook = next((h for h in existing if (h.name or "").startswith(WEBHOOK_PREFIX)), None)
        if hook is None:
            hook = await target.create_webhook(name=f"{WEBHOOK_PREFIX}{name}")

        self._hooks[source_channel.id] = hook
        return hook

    async def forward(self, message: discord.Message) -> bool:
        """Mirror one message. Returns True on success."""
        if getattr(message.guild, "id", None) != self.source_guild_id:
            self.counters["skipped"] += 1
            return False
        if message.author.id == self.client.user.id:
            self.counters["skipped"] += 1
            return False

        hook = await self._webhook_for(message.channel)
        if hook is None:
            self.counters["failed"] += 1
            return False

        try:
            files = []
            for a in (message.attachments or [])[:10]:
                try:
                    files.append(await a.to_file(spoiler=a.is_spoiler()))
                except Exception:
                    log.warning("could not re-upload attachment %s", a.id)

            await hook.send(
                content=message.content or None,
                username=message.author.display_name,
                avatar_url=(str(message.author.display_avatar.url)
                            if message.author.display_avatar else None),
                embeds=message.embeds[:10] if message.embeds else None,
                files=files or None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            self.counters["mirrored"] += 1
            return True
        except discord.HTTPException:
            log.exception("mirror send failed for %s", message.id)
            self.counters["failed"] += 1
            return False
        finally:
            for f in files:
                try:
                    f.close()
                except Exception:
                    pass
