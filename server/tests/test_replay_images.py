import json
from server.sessions.replay import _parse
from server.sessions import events as ev


def test_user_image_block_becomes_usermessage_images(tmp_path):
    # Mirror the CLI's on-disk shape captured by the Task-1 spike.
    line = {"type": "user", "uuid": "u1", "message": {"role": "user", "content": [
        {"type": "text", "text": "see this"},
        {"type": "image", "source": {"type": "base64",
            "media_type": "image/png", "data": "QUJD"}},
    ]}}
    p = tmp_path / "t.jsonl"; p.write_text(json.dumps(line) + "\n")
    out = _parse(str(p), max_turns=20)
    um = next(e for e in out if isinstance(e, ev.UserMessage))
    assert um.text == "see this"
    assert um.images and um.images[0]["mime"] == "image/png"
