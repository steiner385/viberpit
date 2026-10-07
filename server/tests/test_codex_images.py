"""Native image results must become durable attachment URLs before reaching chat."""
import base64

import pytest
from sqlalchemy import select

from server.codex import schema
from server.codex.translator import CodexEventTranslator
from server.db import db_session
from server.db.models import Attachment
from server.sessions.events import ToolResult

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==')


@pytest.mark.asyncio
async def test_native_image_becomes_durable_attachment(isolated_db, tmp_path):
    from server.codex.images import materialize_images
    source = tmp_path / 'shot.png'
    source.write_bytes(PNG)
    item = schema.parse_thread_item({'type': 'imageView', 'id': 'image-1', 'path': str(source)})
    pending = CodexEventTranslator().translate_item(item, turn_id='t1', completed=True)[-1]
    live = await materialize_images(pending, session_id='s1', thread_id='th1')
    assert isinstance(live, ToolResult)
    url = live.payload['content'][0]['source']['url']
    assert url.startswith('/api/attachments/')
    assert str(tmp_path) not in str(live.payload)
    source.unlink()
    replay = await materialize_images(pending, session_id='s1', thread_id='th1')
    assert replay == live
    async with db_session() as db:
        rows = (await db.execute(select(Attachment))).scalars().all()
        assert len(rows) == 1
        assert rows[0].session_id == 's1'


@pytest.mark.asyncio
async def test_missing_image_returns_safe_fallback(isolated_db):
    from server.codex.images import materialize_images
    pending = CodexEventTranslator().translate_item(
        schema.ImageViewItem('bad', '/secret/missing.png'), turn_id='t', completed=True)[-1]
    out = await materialize_images(pending, session_id='s', thread_id='th')
    assert out.payload['content'] == [{'type': 'image'}]
    assert '/secret' not in str(out)


@pytest.mark.asyncio
@pytest.mark.parametrize('native', ['dynamicToolCall', 'mcpToolCall'])
async def test_supported_native_image_content(isolated_db, native):
    from server.codex.images import materialize_images
    encoded = base64.b64encode(PNG).decode()
    item = {'type': native, 'id': 'i', 'tool': 'screenshot', 'status': 'completed', 'arguments': {}}
    if native == 'dynamicToolCall':
        item['contentItems'] = [{'type': 'inputImage', 'imageUrl': f'data:image/png;base64,{encoded}'}]
    else:
        item.update(server='browser', result={'content': [{'type': 'image', 'mimeType': 'image/png', 'data': encoded}]})
    pending = CodexEventTranslator().translate_item(schema.parse_thread_item(item), turn_id='t', completed=True)[-1]
    assert encoded not in repr(pending)
    result = await materialize_images(pending, session_id='s', thread_id='th')
    assert result.payload['content'][0]['source']['url'].startswith('/api/attachments/')
    assert encoded not in str(result.payload)


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['https://credentials.invalid/image.png', 'data:image/png;base64,broken!', 'data:image/png;base64,c2VjcmV0'])
async def test_malformed_or_remote_images_are_not_fetched(isolated_db, source):
    from server.codex.images import PendingToolImages, materialize_images
    result = await materialize_images(
        PendingToolImages(ToolResult('i', True, {}), (('data', source),)),
        session_id='s', thread_id='th')
    assert result.payload['content'] == [{'type': 'image'}]
    assert source not in str(result.payload)


@pytest.mark.asyncio
async def test_replay_uses_live_attachment_after_source_removed(isolated_db, tmp_path):
    from server.codex.images import materialize_images
    from server.codex.replay import CodexThreadReplay
    from server.tests.test_codex_replay import _FakeClient, _turn
    source = tmp_path / 'screenshot.png'
    source.write_bytes(PNG)
    item = {'type': 'imageView', 'id': 'i', 'path': str(source)}
    pending = CodexEventTranslator().translate_item(schema.parse_thread_item(item), turn_id='t', completed=True)[-1]
    live = await materialize_images(pending, session_id='s', thread_id='thread-1')
    source.unlink()
    replay = CodexThreadReplay(_FakeClient([_turn('t', extra=[item])]), 'thread-1', session_id='s')
    for _ in range(2):
        window = await replay.window()
        results = [e for e in window.events if isinstance(e, ToolResult)]
        assert results[0].payload == live.payload
    async with db_session() as db:
        assert len((await db.execute(select(Attachment))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_concurrent_live_and_replay_share_one_attachment(isolated_db, tmp_path):
    import asyncio
    from server.codex.images import materialize_images
    source = tmp_path / 'shot.png'
    source.write_bytes(PNG)
    pending = CodexEventTranslator().translate_item(
        schema.ImageViewItem('i', str(source)), turn_id='t', completed=True)[-1]
    results = await asyncio.gather(*[
        materialize_images(pending, session_id='s', thread_id='th') for _ in range(3)])
    assert results[0].payload['content'][0]['type'] == 'image'
    assert all(r.payload == results[0].payload for r in results)
    async with db_session() as db:
        assert len((await db.execute(select(Attachment))).scalars().all()) == 1
