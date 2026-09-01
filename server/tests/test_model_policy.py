"""Tests for resolve_model_policy — the model-approval policy resolver.

Fixture note: the codebase has no db_session_factory fixture.  The standard
pattern for tests that need a real async DB session is asyncio.run() wrapping
init_db() + db_session() (see test_archived_at.py, test_autopilot_context.py).
Each test gets its own isolated file-backed SQLite DB via tmp_path.
"""
from __future__ import annotations

import asyncio
import os

from server.sessions.model_policy import resolve_model_policy, VALID_ACTIONS
from server.db.models import AppSettings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(tmp_path, coro_fn):
    """Run *coro_fn(db)* in an isolated DB; unset model-approval env vars so
    live shell settings don't skew the default-precedence tests.
    """
    from server.db import init_db, db_session

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'policy_test.db'}"

    async def _run_async():
        await init_db(db_url=db_url)
        async with db_session() as db:
            return await coro_fn(db)

    # Truly unset the two model-approval env vars (don't set to ""; that would
    # be picked up as the configured value and override the Settings defaults).
    saved = {}
    for key in ("CCSWITCH_MODEL_APPROVAL_ACTION", "CCSWITCH_MODEL_APPROVAL_DENYLIST"):
        if key in os.environ:
            saved[key] = os.environ.pop(key)
    try:
        return asyncio.run(_run_async())
    finally:
        os.environ.update(saved)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_non_gated_is_allow(tmp_path):
    async def _check(db):
        r1 = await resolve_model_policy(db, model="opus")
        r2 = await resolve_model_policy(db, model=None)
        return r1, r2

    # opus IS in the default denylist ("fable"), but opus is NOT fable — expect allow
    # Wait: the default denylist is "fable", so "opus" tier is NOT gated → allow.
    r1, r2 = _run(tmp_path, _check)
    assert r1 == "allow"
    assert r2 == "allow"


def test_gated_default_requires_approval(tmp_path):
    async def _check(db):
        r1 = await resolve_model_policy(db, model="fable")
        r2 = await resolve_model_policy(db, model="claude-fable-5")
        r3 = await resolve_model_policy(db, model="fable[1m]")
        return r1, r2, r3

    r1, r2, r3 = _run(tmp_path, _check)
    assert r1 == "require_approval"
    assert r2 == "require_approval"
    assert r3 == "require_approval"


def test_global_action_override(tmp_path):
    async def _check(db):
        db.add(AppSettings(id="singleton", model_approval_action="block"))
        await db.commit()
        return await resolve_model_policy(db, model="fable")

    result = _run(tmp_path, _check)
    assert result == "block"


def test_global_denylist_override(tmp_path):
    async def _check(db):
        # Override denylist to only "haiku" — fable is no longer gated
        db.add(AppSettings(id="singleton", model_approval_denylist="haiku"))
        await db.commit()
        r_fable = await resolve_model_policy(db, model="fable")
        r_haiku = await resolve_model_policy(db, model="haiku")
        return r_fable, r_haiku

    r_fable, r_haiku = _run(tmp_path, _check)
    assert r_fable == "allow"      # no longer gated
    assert r_haiku == "require_approval"


def test_valid_actions():
    assert VALID_ACTIONS == frozenset({"allow", "require_approval", "block"})
