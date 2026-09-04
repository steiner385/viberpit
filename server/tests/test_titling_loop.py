"""Tests for the periodic titling sweep — adopts late-written titles with no new turn."""
import asyncio

from server.db import init_db, db_session
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


async def _seed(rows):
    async with db_session() as db:
        for r in rows:
            db.add(DbSession(**r))


async def _build_orch(tmp_path, *, title_fn=None, **row):
    db_url = f"sqlite+aiosqlite:///{tmp_path/'ccswitch.db'}"
    await init_db(db_url=db_url)
    base = dict(id="s1", name="New Session", account_name="acct1",
                claude_session_id="claude-s1", status="in_progress", cwd="/repo/s1")
    base.update(row)
    await _seed([base])
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), title_fn=title_fn)
    await orch.rehydrate()
    return orch


def test_titling_sweep_adopts_title_without_a_turn(tmp_path):
    transcript = tmp_path / "claude-s1.jsonl"
    transcript.write_text('{"type":"ai-title","aiTitle":"Investigate slow query"}\n')

    async def run():
        orch = await _build_orch(tmp_path)
        orch.transcript_path = lambda sid: str(transcript)
        await orch._titling_sweep()           # no _persist_status / no "done"
        return orch.record("s1").name

    assert asyncio.run(run()) == "Investigate slow query"


def test_titling_sweep_skips_ended_sessions(tmp_path):
    transcript = tmp_path / "claude-s1.jsonl"
    transcript.write_text('{"type":"ai-title","aiTitle":"Should not apply"}\n')

    async def run():
        # Seed as idle so rehydrate loads it, then simulate mid-run transition to ended.
        orch = await _build_orch(tmp_path, status="idle")
        orch.record("s1").status = "ended"
        orch.transcript_path = lambda sid: str(transcript)
        await orch._titling_sweep()
        return orch.record("s1").name

    assert asyncio.run(run()) == "New Session"


def test_titling_sweep_generates_title_when_no_ai_title(tmp_path):
    """SDK-driven sessions get no Claude ai-title record, so the sweep must fall
    back to summarizing the user prompts via the injected title generator."""
    transcript = tmp_path / "claude-s1.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"role":"user","content":"fix the nightly build"}}\n'
    )

    seen = {}

    async def stub_title_fn(prompts, *, account, cwd):
        seen["prompts"] = prompts
        seen["account"] = account
        return "Fix the nightly build"

    async def run():
        # cwd must exist: the fallback generator runs a query in it and now skips a
        # vanished cwd (test_titling_sweep_skips_when_cwd_missing).
        orch = await _build_orch(tmp_path, title_fn=stub_title_fn, cwd=str(tmp_path))
        orch.transcript_path = lambda sid: str(transcript)
        await orch._titling_sweep()
        return orch.record("s1").name

    assert asyncio.run(run()) == "Fix the nightly build"
    assert seen["prompts"] == ["fix the nightly build"]
    assert seen["account"] == "acct1"


def test_titling_sweep_prefers_ai_title_over_generator(tmp_path):
    """When Claude *did* write an ai-title, the generator must not be called."""
    transcript = tmp_path / "claude-s1.jsonl"
    transcript.write_text(
        '{"type":"ai-title","aiTitle":"Investigate slow query"}\n'
        '{"type":"user","message":{"role":"user","content":"something else"}}\n'
    )

    called = {"n": 0}

    async def stub_title_fn(prompts, *, account, cwd):
        called["n"] += 1
        return "Generated should not win"

    async def run():
        orch = await _build_orch(tmp_path, title_fn=stub_title_fn)
        orch.transcript_path = lambda sid: str(transcript)
        await orch._titling_sweep()
        return orch.record("s1").name

    assert asyncio.run(run()) == "Investigate slow query"
    assert called["n"] == 0


def test_titling_interval_defaults_to_15s(tmp_path):
    async def run():
        orch = await _build_orch(tmp_path)
        return orch._titling_interval
    assert asyncio.run(run()) == 15.0


def test_titling_sweep_does_not_regenerate_when_transcript_unchanged(tmp_path):
    """A repeated sweep over an idle session (no new prompts) must not re-run the
    fallback title generator — see #1013: an idle session's transcript is static
    between sweeps, so re-querying every _titling_interval wastes a Haiku call
    (and the `claude -p` helper subprocess it spawns) for no reason."""
    transcript = tmp_path / "claude-s1.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"role":"user","content":"fix the nightly build"}}\n'
    )

    called = {"n": 0}

    async def stub_title_fn(prompts, *, account, cwd):
        called["n"] += 1
        return "Fix the nightly build"

    async def run():
        orch = await _build_orch(tmp_path, title_fn=stub_title_fn, cwd=str(tmp_path))
        orch.transcript_path = lambda sid: str(transcript)
        await orch._titling_sweep()
        await orch._titling_sweep()
        await orch._titling_sweep()
        return called["n"]

    assert asyncio.run(run()) == 1


def test_titling_sweep_regenerates_when_transcript_changes(tmp_path):
    """A genuinely new user prompt must still trigger a fresh title attempt even
    though a previous sweep already generated one for the earlier prompt."""
    transcript = tmp_path / "claude-s1.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"role":"user","content":"fix the nightly build"}}\n'
    )

    called = {"n": 0}

    async def stub_title_fn(prompts, *, account, cwd):
        called["n"] += 1
        return f"Generated {called['n']}"

    async def run():
        orch = await _build_orch(tmp_path, title_fn=stub_title_fn, cwd=str(tmp_path))
        orch.transcript_path = lambda sid: str(transcript)
        await orch._titling_sweep()
        transcript.write_text(
            '{"type":"user","message":{"role":"user","content":"fix the nightly build"}}\n'
            '{"type":"user","message":{"role":"user","content":"also fix the flaky test"}}\n'
        )
        await orch._titling_sweep()
        return called["n"]

    assert asyncio.run(run()) == 2


def test_titling_sweep_skips_when_cwd_missing(tmp_path):
    """A session whose worktree vanished (removed/archived out from under a still-active
    record) must NOT run the cwd-bound fallback title query — it errors every sweep,
    producing the 'Working directory does not exist' spam. Skip it quietly."""
    transcript = tmp_path / "claude-s1.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"role":"user","content":"do the thing"}}\n'
    )

    called = {"n": 0}

    async def stub_title_fn(prompts, *, account, cwd):
        called["n"] += 1
        return "should never be applied"

    async def run():
        orch = await _build_orch(
            tmp_path, title_fn=stub_title_fn, cwd=str(tmp_path / "gone-worktree")
        )
        orch.transcript_path = lambda sid: str(transcript)
        await orch._titling_sweep()  # must not raise
        return called["n"], orch.record("s1").name

    n_calls, name = asyncio.run(run())
    assert n_calls == 0  # the cwd-bound generator is never invoked
    assert name == "New Session"  # name left unchanged
