import base64
from server.sessions.attachments import (
    sniff_media_type, ext_for, image_block, MAX_BYTES, MAX_PER_MESSAGE,
)

PNG = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 16
JPEG = bytes.fromhex("ffd8ff") + b"\x00" * 16
GIF = b"GIF89a" + b"\x00" * 16
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 8

def test_sniff_known_types():
    assert sniff_media_type(PNG) == "image/png"
    assert sniff_media_type(JPEG) == "image/jpeg"
    assert sniff_media_type(GIF) == "image/gif"
    assert sniff_media_type(WEBP) == "image/webp"

def test_sniff_rejects_unknown():
    assert sniff_media_type(b"%PDF-1.7") is None
    assert sniff_media_type(b"") is None

def test_ext_for():
    assert ext_for("image/jpeg") == "jpg"
    assert ext_for("image/png") == "png"

def test_image_block_shape():
    blk = image_block("image/png", "AAAA")
    assert blk == {"type": "image",
                   "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}

def test_limits_constants():
    assert MAX_BYTES == 10 * 1024 * 1024
    assert MAX_PER_MESSAGE == 5
