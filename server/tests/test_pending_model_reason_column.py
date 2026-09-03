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
    """Behavioral test: call _record_from_row with a row carrying pending_model_reason
    and verify the returned SessionRecord has it set. This guards against silent drops
    during daemon restart/unarchive."""
    from types import SimpleNamespace
    from server.sessions.orchestrator import SessionOrchestrator

    # Create a mock row object with the minimal attributes _record_from_row needs
    mock_row = SimpleNamespace(
        id="test-session-id",
        name="Test Session",
        account_name="test-account",
        claude_session_id="claude-123",
        cwd="/tmp/test",
        repo="owner/repo",
        status="idle",
        name_is_auto=False,
        permission_mode="interactive",
        # The field we're testing:
        pending_model_reason="User requested for code review",
        # Other fields that _record_from_row accesses via getattr with defaults
        branch=None,
        origin=None,
        origin_ref=None,
        assisted_by=None,
        created_at=None,
        updated_at=None,
        last_turn_at=None,
        adopted_in_place=False,
        model=None,
        pending_model=None,
        account_pinned=False,
        active_goal=None,
        inferred_goal=None,
        autopilot_paused=False,
        auto_archive_opt_out_reason=None,
        authored_branches=None,
        context_used_tokens=None,
        context_max_tokens=None,
        command_catalog=None,
        pr_resolution=None,
        pr_resolution_source=None,
        host=None,
    )

    # Create an orchestrator instance to access the _record_from_row method
    # (It doesn't need any complex setup since we're just testing one method)
    orch = SessionOrchestrator(sdk_factory=None, poll_interval=1, usage_fn=None, repo_root="/tmp")

    # Call the function
    record = orch._record_from_row(mock_row)

    # Verify the pending_model_reason survived the conversion
    assert record.pending_model_reason == "User requested for code review", (
        f"pending_model_reason was lost in _record_from_row: "
        f"expected 'User requested for code review', got {record.pending_model_reason}"
    )


def test_orchestrator_record_from_row_preserves_pending_model_requested_by():
    """Behavioral test: call _record_from_row with a row carrying
    pending_model_requested_by and verify the returned SessionRecord has it set.
    This is the same field-drop hazard pending_model_reason hit (silently dropped on
    daemon restart/unarchive because _record_from_row's row mapping is the single
    source of truth for rebuilding a SessionRecord and is easy to forget a field in)."""
    from types import SimpleNamespace
    from server.sessions.orchestrator import SessionOrchestrator

    mock_row = SimpleNamespace(
        id="test-session-id",
        name="Test Session",
        account_name="test-account",
        claude_session_id="claude-123",
        cwd="/tmp/test",
        repo="owner/repo",
        status="idle",
        name_is_auto=False,
        permission_mode="interactive",
        pending_model_reason="needs a human",
        # The field we're testing:
        pending_model_requested_by="session:self",
        # Other fields that _record_from_row accesses via getattr with defaults
        branch=None,
        origin=None,
        origin_ref=None,
        assisted_by=None,
        created_at=None,
        updated_at=None,
        last_turn_at=None,
        adopted_in_place=False,
        model=None,
        pending_model="fable",
        account_pinned=False,
        active_goal=None,
        inferred_goal=None,
        autopilot_paused=False,
        auto_archive_opt_out_reason=None,
        authored_branches=None,
        context_used_tokens=None,
        context_max_tokens=None,
        command_catalog=None,
        pr_resolution=None,
        pr_resolution_source=None,
        host=None,
    )

    orch = SessionOrchestrator(sdk_factory=None, poll_interval=1, usage_fn=None, repo_root="/tmp")

    record = orch._record_from_row(mock_row)

    assert record.pending_model_requested_by == "session:self", (
        f"pending_model_requested_by was lost in _record_from_row: "
        f"expected 'session:self', got {record.pending_model_requested_by}"
    )


def test_migration_chain_has_exactly_one_head():
    """Parse all migration files and verify exactly one head (one revision that no other
    revision names as its down_revision). Assert the head is 0016_host_heartbeats."""
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
    assert heads[0] == "0016_host_heartbeats", f"Expected head to be 0016_host_heartbeats, got {heads[0]}"
