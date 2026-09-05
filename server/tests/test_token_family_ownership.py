"""Token-family ownership: who may refresh an account's OAuth token, and when.

Two accounts lost OAuth in production after weeks of stability (2026-09-04). Root
cause: the idle-token sweep refreshed from ccswitch's STORE SLOT via
``ensure_valid_token``, the one entry point with no ``sync_back`` recovery. While a
session is live the ``claude`` SDK subprocess owns and rotates the token family, so
the slot holds a SUPERSEDED refresh token. The moment the session ended the account
became "idle", this sweep refreshed from that stale slot, got 400 invalid_grant —
which ``oauth.refresh_access_token`` treats as proof the grant is dead — and the
account flipped to needs-reauth.

Compounding it: ``has_live_session`` only consulted this process's ``_drivers``, so a
session running on a fleet worker read as "not live" on the primary and its token
family was refreshed out from under the worker's running SDK.
"""
from datetime import datetime
from unittest import mock

import pytest

from server.db.models import CAPACITY_EXEMPT_STATUSES
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord


def _orch():
    return SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda n: None)


@pytest.fixture(autouse=True)
def _fixed_host_identity():
    """Pin this daemon's fleet identity so "remote" is unambiguous in CI."""
    with mock.patch("server.sessions.orchestrator.self_host_name", return_value="dobby"):
        yield


def _record(sid: str, account: str, status: str, host: str | None) -> SessionRecord:
    now = datetime.utcnow()
    return SessionRecord(
        id=sid, name=sid, account=account, claude_session_id="",
        cols=80, rows=24, cwd="/tmp", status=status,
        created_at=now, updated_at=now, host=host)


@pytest.mark.asyncio
async def test_idle_sweep_uses_the_rotation_recovering_entry_point():
    """The idle sweep must call acquire_valid_token, never ensure_valid_token.

    acquire_valid_token is the ONLY entry point that, on AuthError, runs sync_back to
    pull a rotation the SDK already performed into the slot before giving up. A
    just-ended session's account is precisely the case where the slot is stale, so
    the unprotected path turns a healthy account into needs-reauth. Every other
    caller in the tree already uses acquire_valid_token.
    """
    orch = _orch()
    orch.has_live_session = lambda name: False
    with mock.patch("server.sessions.orchestrator.ccswitch_accounts") as ca:
        ca.list_accounts.return_value = [{"name": "just_ended", "status": "ok"}]
        await orch._refresh_idle_tokens_once()
    assert [c.args[0] for c in ca.acquire_valid_token.call_args_list] == ["just_ended"]
    assert ca.ensure_valid_token.call_args_list == [], (
        "idle sweep must not use the entry point that skips sync_back recovery")


def test_has_live_session_sees_a_session_live_on_another_fleet_host():
    """Token-family ownership is fleet-wide, not host-local.

    The primary deliberately holds every fleet row in _records (see _owns_record), but
    has_live_session only consulted _drivers — this process's own drivers. A session
    running on a worker therefore read as "not live", so the poller and the idle sweep
    refreshed a token family the worker's SDK subprocess owns. Unlike its sibling
    accessors (live_session_load / live_session_counts, both merged across hosts via
    an async RPC fan-out) this one is consulted from a threadpool thread, so the
    fleet-wide answer has to come from the already-mirrored _records, synchronously.
    """
    orch = _orch()
    orch._records["remote1"] = _record("remote1", "acct_on_worker", "in_progress", "rocky")
    assert orch.has_live_session("acct_on_worker") is True


@pytest.mark.parametrize("status", sorted(CAPACITY_EXEMPT_STATUSES))
def test_has_live_session_ignores_remote_sessions_holding_no_driver(status):
    """A row that holds no live driver must NOT pin the token family forever.

    Such rows linger in the primary's _records; treating them as live would
    permanently disable proactive refresh for that account — the opposite failure.

    CAPACITY_EXEMPT_STATUSES is the documented single source of truth for "holds no
    live driver" (its stated invariant: a session holding a live driver must not be
    capacity-exempt), so this reuses it rather than re-inlining a status set.
    """
    orch = _orch()
    orch._records["r1"] = _record("r1", "quiet_acct", status, "rocky")
    assert orch.has_live_session("quiet_acct") is False


@pytest.mark.parametrize("status", ["done", "idle", "in_progress", "waiting_input"])
def test_has_live_session_counts_a_remote_session_between_turns(status):
    """`done` and `idle` are LIVE — the warm driver (and its token) is still held.

    Per CAPACITY_EXEMPT_STATUSES' rationale, `done` is returned after ANY ended turn,
    so a `done` session on a worker keeps its driver for the next prompt. Refreshing
    its account rotates the token family out from under that running process, which is
    the exact production failure this guards.
    """
    orch = _orch()
    orch._records["r1"] = _record("r1", "busy_acct", status, "rocky")
    assert orch.has_live_session("busy_acct") is True


def test_has_live_session_still_true_for_a_local_driver():
    """The original contract is preserved: a driver in THIS process counts as live
    even with no matching _records row."""
    orch = _orch()
    orch._drivers["local1"] = mock.MagicMock(account="local_acct")
    assert orch.has_live_session("local_acct") is True


def test_has_live_session_false_for_an_account_with_nothing_running():
    orch = _orch()
    orch._records["other"] = _record("other", "someone_else", "in_progress", "rocky")
    assert orch.has_live_session("idle_acct") is False


@pytest.mark.parametrize("host", ["dobby", None])
def test_has_live_session_ignores_local_rows_with_no_driver(host):
    """For THIS host, _drivers is authoritative — a local row must not override it.

    Falling back to _records locally could only add false positives: a lingering
    local row whose process is gone would suppress proactive refresh for that account
    forever. `host=None` (unstamped, pre-fleet rows) is treated as local for the same
    reason. Remote rows are the only ones whose drivers we cannot see.
    """
    orch = _orch()
    orch._records["local1"] = _record("local1", "local_acct", "in_progress", host)
    assert orch.has_live_session("local_acct") is False
