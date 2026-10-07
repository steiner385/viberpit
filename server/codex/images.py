"""Materialize native tool images before emitting normalized events.

PendingToolImages is internal to the adapter: filesystem paths and data URLs never
reach the event bus. Live and native replay use the same attachment identity.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from server.db import db_session
from server.db.models import Attachment
from server.mcp_spec import ToolArgError
from server.sessions.artifacts import IMAGE_MEDIA_TYPES, read_image
from server.sessions.attachments import MAX_BYTES, sniff_media_type, store_attachment
from server.sessions.events import ToolResult


@dataclass(frozen=True, repr=False)
class PendingToolImages:
    result: ToolResult
    sources: tuple[tuple[str, str], ...]


def _read_source(kind: str, value: str) -> tuple[bytes, str]:
    if kind == 'path':
        return read_image(value)
    if len(value) > (MAX_BYTES * 4 // 3) + 1024:
        raise ValueError('image too large')
    header, separator, encoded = value.partition(',')
    if not separator or not header.startswith('data:image/') or not header.endswith(';base64'):
        raise ValueError('unsupported image source')
    data = base64.b64decode(encoded, validate=True)
    media_type = sniff_media_type(data[:12])
    if len(data) > MAX_BYTES or media_type not in IMAGE_MEDIA_TYPES:
        raise ValueError('invalid image')
    return data, media_type


async def materialize_images(event: object, *, session_id: str | None,
                             thread_id: str, identity: str = 'session') -> object:
    if not isinstance(event, PendingToolImages):
        return event
    content = list(event.result.payload.get('content', []))
    for index, (kind, value) in enumerate(event.sources):
        # Native ids remain inside the adapter; only this opaque attachment UUID
        # becomes public. A saved row wins over a source that changed/disappeared.
        aid = str(uuid5(NAMESPACE_URL, f'viberpit:image:{session_id}:{thread_id}:{event.result.tool_id}:{index}'))
        try:
            if session_id is None:
                raise ValueError('session unavailable')
            async with db_session() as db:
                row = await db.get(Attachment, aid)
                if row is None:
                    data, media_type = await asyncio.to_thread(_read_source, kind, value)
                    row = await store_attachment(
                        db, data, media_type, session_id=session_id, identity=identity,
                        filename=Path(value).name[:255] if kind == 'path' else 'image',
                        attachment_id=aid)
                content.append({'type': 'image', 'source': {
                    'type': 'url', 'url': f'/api/attachments/{row.id}'}})
        except (ToolArgError, OSError, ValueError, binascii.Error):
            # Retain the image's place in the transcript: the canonical image
            # renderer shows its unavailable state even when the tool collapses.
            content.append({'type': 'image'})
    return replace(event.result, payload={**event.result.payload, 'content': content})
