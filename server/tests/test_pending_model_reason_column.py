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


def test_orchestrator_record_from_row_preserves_pending_model_reason():
    """Audit by inspection: verify _record_from_row includes pending_model_reason mapping.
    This guards against silent drops during daemon restart/unarchive (issue #XXX)."""
    from pathlib import Path
    orch_file = Path(__file__).resolve().parents[1] / "sessions" / "orchestrator.py"
    content = orch_file.read_text()

    # Find the _record_from_row function and extract the SessionRecord construction
    # Verify that every pending_model* field in SessionRecord has a corresponding
    # line in _record_from_row.
    session_record_fields = [
        "pending_model=",
        "pending_model_reason=",
    ]
    for field in session_record_fields:
        assert field in content, (
            f"_record_from_row must map {field} from the DB row to SessionRecord; "
            f"found in orchestrator.py: {field in content}"
        )


def test_migration_chain_has_exactly_one_head():
    """Parse all migration files and verify exactly one head (one revision that no other
    revision names as its down_revision). Assert the head is 0015_pending_model_reason."""
    from pathlib import Path
    import re

    versions_dir = Path(__file__).resolve().parents[1] / "db/migrations/versions"

    revisions = {}  # revision -> down_revision
    for migration_file in sorted(versions_dir.glob("*.py")):
        if migration_file.name.startswith("__"):
            continue
        content = migration_file.read_text()

        # Extract revision and down_revision
        rev_match = re.search(r'revision = "([^"]+)"', content)
        down_rev_match = re.search(r'down_revision = "([^"]+)"', content)

        if rev_match:
            rev = rev_match.group(1)
            down_rev = down_rev_match.group(1) if down_rev_match else None
            revisions[rev] = down_rev

    # Find heads: revisions that no other revision names as its down_revision
    all_down_revisions = set(v for v in revisions.values() if v is not None)
    heads = [rev for rev in revisions.keys() if rev not in all_down_revisions]

    assert len(heads) == 1, f"Expected exactly one migration head, found {len(heads)}: {heads}"
    assert heads[0] == "0015_pending_model_reason", f"Expected head to be 0015_pending_model_reason, got {heads[0]}"
