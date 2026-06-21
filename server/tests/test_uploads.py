import base64, os
from pathlib import Path
import pytest
from server.sessions import uploads

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))

def test_sniff_png():
    assert uploads.sniff_mime(PNG) == "image/png"

def test_sniff_rejects_non_image():
    assert uploads.sniff_mime(b"not an image") is None

def test_stage_then_associate_then_read():
    ref = uploads.stage_image("image/png", PNG)
    assert ref["mime"] == "image/png" and len(ref["sha"]) == 64
    assert uploads.staging_path(ref["sha"]).exists()
    dest = uploads.associate("sess1", ref["sha"], ref["mime"])
    assert dest.exists() and dest.parent.name == "sess1"
    assert uploads.staging_path(ref["sha"]) is None  # moved out of staging
    assert base64.b64decode(uploads.read_b64(dest)) == PNG

def test_associate_idempotent():
    ref = uploads.stage_image("image/png", PNG)
    d1 = uploads.associate("sess1", ref["sha"], ref["mime"])
    d2 = uploads.associate("sess1", ref["sha"], ref["mime"])  # already associated
    assert d1 == d2

def test_stage_rejects_oversize():
    big = PNG + b"\x00" * (uploads.MAX_BYTES + 1)
    with pytest.raises(uploads.UploadError):
        uploads.stage_image("image/png", big)

def test_stage_rejects_type_mismatch():
    with pytest.raises(uploads.UploadError):
        uploads.stage_image("image/png", b"GIF89a-but-claims-png")

def test_sweep_staging_removes_old(monkeypatch):
    ref = uploads.stage_image("image/png", PNG)
    p = uploads.staging_path(ref["sha"])
    old = p.stat().st_mtime - 7 * 3600
    os.utime(p, (old, old))
    assert uploads.sweep_staging(max_age_h=6.0) == 1
    assert uploads.staging_path(ref["sha"]) is None
