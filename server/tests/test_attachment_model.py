from __future__ import annotations
import sys
from pathlib import Path
REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

def test_attachments_dir_under_root(tmp_path, monkeypatch):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))
    from importlib import reload
    from ccswitch import paths
    reload(paths)
    d = paths.attachments_dir()
    assert d == tmp_path / "attachments"

def test_attachment_model_columns():
    from server.db.models import Attachment
    cols = set(Attachment.__table__.columns.keys())
    assert {"id", "session_id", "turn_id", "media_type", "filename",
            "byte_size", "sha256", "uploaded_by", "created_at"} <= cols
    assert Attachment.__table__.c.session_id.nullable is True
    assert Attachment.__table__.c.turn_id.nullable is True
