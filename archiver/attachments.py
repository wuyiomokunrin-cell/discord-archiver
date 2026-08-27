"""Attachment handling.

Discord CDN URLs are signed and expire, so storing the URL alone produces an
archive that rots. This module downloads the bytes, content-addresses them by
SHA-256, and dedupes identical files across messages.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from typing import Any, Mapping

_UNSAFE = re.compile(r"[^\w.\-]+")


def safe_filename(name: str | None, fallback: str = "attachment") -> str:
    """Sanitise a filename for the local filesystem."""
    if not name:
        return fallback
    cleaned = _UNSAFE.sub("_", name.strip()).strip("._")
    return cleaned[:180] or fallback


def shard_path(root: str | Path, sha256: str, filename: str) -> Path:
    """Content-addressed path: data/attachments/ab/abcd1234.../picture.png

    Sharding by the first two hex chars keeps any one directory small.
    """
    root = Path(root)
    return root / sha256[:2] / sha256 / safe_filename(filename)


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def download_one(session, url: str, dest: Path,
                       timeout: float = 60.0, retries: int = 3) -> tuple[bytes, int]:
    """Download a URL with simple backoff. Returns (bytes, status)."""
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            async with session.get(url, timeout=timeout) as resp:
                if resp.status == 200:
                    return await resp.read(), resp.status
                last_err = RuntimeError(f"HTTP {resp.status} for {url}")
        except Exception as exc:  # noqa: BLE001 - network errors are varied
            last_err = exc
        await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"failed after {retries} attempts: {last_err}")


async def process_pending(db, attachments_dir: str | Path,
                          max_concurrent: int = 5,
                          max_bytes: int | None = None) -> dict[str, int]:
    """Download every attachment marked 'pending' in the database.

    Deduplicates on SHA-256: if we already have identical bytes on disk, the new
    row points at the existing file instead of storing a second copy.
    """
    import aiohttp

    attachments_dir = Path(attachments_dir)
    attachments_dir.mkdir(parents=True, exist_ok=True)
    stats = {"downloaded": 0, "deduped": 0, "failed": 0, "skipped_too_large": 0}
    sem = asyncio.Semaphore(max_concurrent)

    pending = db.pending_attachments(limit=10_000)
    if not pending:
        return stats

    async with aiohttp.ClientSession() as session:

        async def handle(att) -> None:
            async with sem:
                if max_bytes and att["size"] and att["size"] > max_bytes:
                    db.set_attachment_failed(att["id"])
                    stats["skipped_too_large"] += 1
                    return
                try:
                    data, _ = await download_one(session, att["url"], None)
                except Exception:
                    db.set_attachment_failed(att["id"])
                    stats["failed"] += 1
                    return

                digest = sha256_of(data)
                existing = db.find_attachment_by_sha(digest)
                if existing and existing["local_path"] and Path(existing["local_path"]).exists():
                    db.set_attachment_downloaded(att["id"], existing["local_path"],
                                                 digest, len(data))
                    stats["deduped"] += 1
                    return

                dest = shard_path(attachments_dir, digest, att["filename"])
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                db.set_attachment_downloaded(att["id"], str(dest), digest, len(data))
                stats["downloaded"] += 1

        await asyncio.gather(*(handle(a) for a in pending))

    return stats


def record_attachments(db, message_id: str, channel_id: str,
                       attachments) -> int:
    """Insert attachment rows for a discord.Message. Returns how many were new."""
    new = 0
    for a in attachments:
        was_new = db.add_attachment({
            "id": a.id,
            "message_id": message_id,
            "channel_id": channel_id,
            "filename": a.filename,
            "url": a.url,
            "local_path": None,
            "size": a.size,
            "content_type": getattr(a, "content_type", None),
            "width": getattr(a, "width", None),
            "height": getattr(a, "height", None),
            "sha256": None,
            "download_status": "pending",
        })
        new += int(was_new)
    return new
