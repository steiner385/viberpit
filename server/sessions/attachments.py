"""Pure helpers for image attachments — no I/O, no DB."""
from __future__ import annotations

MAX_BYTES = 10 * 1024 * 1024
MAX_PER_MESSAGE = 5

_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}


def sniff_media_type(head: bytes) -> str | None:
    """Detect image type from leading bytes. Returns a media type or None."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "image/gif"
    if len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def ext_for(media_type: str) -> str:
    return _EXT.get(media_type, "bin")


def image_block(media_type: str, data_b64: str) -> dict:
    return {"type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data_b64}}
