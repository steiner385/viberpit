"""pending_model_reason: nullable column, record round-trip, API surface."""
from server.db.models import Session as DbSession
from server.sessions.types import SessionRecord


def test_column_exists_and_is_nullable():
    col = DbSession.__table__.columns["pending_model_reason"]
    assert col.nullable is True


def test_record_round_trips_the_reason():
    rec = SessionRecord(id="s1", name="test", account="test", claude_session_id="c1",
                        cols=80, rows=24, cwd="/tmp", pending_model="fable",
                        pending_model_reason="cross-module refactor")
    revived = SessionRecord.from_dict(rec.to_dict())
    assert revived.pending_model_reason == "cross-module refactor"


def test_record_defaults_to_none():
    rec = SessionRecord(id="s1", name="test", account="test", claude_session_id="c1",
                        cols=80, rows=24, cwd="/tmp")
    assert rec.pending_model_reason is None


def test_migration_chains_off_the_current_head():
    """Read the file rather than importing it — the module name starts with a digit."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "db/migrations/versions"
           / "0014_pending_model_reason.py").read_text()
    assert 'revision = "0014_pending_model_reason"' in src
    assert 'down_revision = "0013_host_upgrade_ssh_config"' in src
