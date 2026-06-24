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
    assert um.images[0]["url"] == "data:image/png;base64,QUJD"
    assert um.images[0]["name"] == ""


def test_user_image_only_turn_emits_usermessage(tmp_path):
    # Image-only turn: content is purely an image block, no text block.
    line = {"type": "user", "uuid": "u2", "message": {"role": "user", "content": [
        {"type": "image", "source": {"type": "base64",
            "media_type": "image/png", "data": "QUJD"}},
    ]}}
    p = tmp_path / "img_only.jsonl"; p.write_text(json.dumps(line) + "\n")
    out = _parse(str(p), max_turns=20)
    um = next(e for e in out if isinstance(e, ev.UserMessage))
    assert um.text == ""
    assert um.images and um.images[0]["mime"] == "image/png"


def test_image_block_with_empty_data_is_skipped(tmp_path):
    # Transcripts can carry an image block whose base64 payload was elided/truncated
    # (empty or missing `data`). Emitting `data:image/png;base64,` with no payload
    # yields a structurally invalid URL the browser rejects (net::ERR_INVALID_URL).
    # Such blocks must be dropped, not rendered. Here the text keeps the bubble alive.
    line = {"type": "user", "uuid": "u3", "message": {"role": "user", "content": [
        {"type": "text", "text": "see this"},
        {"type": "image", "source": {"type": "base64",
            "media_type": "image/png", "data": ""}},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png"}},
    ]}}
    p = tmp_path / "empty_img.jsonl"; p.write_text(json.dumps(line) + "\n")
    out = _parse(str(p), max_turns=20)
    um = next(e for e in out if isinstance(e, ev.UserMessage))
    assert um.text == "see this"
    assert um.images is None  # both payload-less blocks dropped → no image list


def test_image_only_turn_with_empty_data_emits_no_bubble(tmp_path):
    # If the ONLY content is a payload-less image, there is nothing to render at all.
    line = {"type": "user", "uuid": "u4", "message": {"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": ""}},
    ]}}
    p = tmp_path / "empty_img_only.jsonl"; p.write_text(json.dumps(line) + "\n")
    out = _parse(str(p), max_turns=20)
    assert not any(isinstance(e, ev.UserMessage) for e in out)
