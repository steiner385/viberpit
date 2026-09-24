"""Tests for the dispatch-time "wrong repository" check.

Two layers:
  - POST /api/dispatch/repo-check, tested via TestClient with a valid
    Tailscale-User-Login identity header and ``suggest_repo`` /
    ``recent_repo_vocabulary`` patched on the fully-qualified names the router
    imports (no real SDK calls).
  - ``server.sessions.repo_match`` itself: ``suggest_repo`` against a fake
    ``_query_fn`` stream (fail-open on every bad reply) and
    ``recent_repo_vocabulary`` against seeded ``Session`` rows.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from server.db import db_session
from server.db.models import Session as DbSession
from server.sessions.repo_match import (
    RepoSuggestion,
    RepoVocabulary,
    recent_repo_vocabulary,
    repo_check_enabled,
    suggest_repo,
)

IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}


def _make_client() -> TestClient:
    """Build a minimal test app with TestClient (lifespan-aware).

    The DB URL comes from the ``isolated_db`` fixture (a per-test SQLite file),
    so every test below takes that fixture rather than patching the env here.
    """
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
    app = create_app(orchestrator=orch)
    return TestClient(app, raise_server_exceptions=False)


VOCAB = [
    RepoVocabulary(repo="steiner385/viberpit", titles=["Fix dispatch bar", "Daemon drain"]),
    RepoVocabulary(repo="cairnea/KinDash", titles=["Task modal footer", "Roadmap tree"]),
]


def _fake_query(reply_text: str, *, calls: list | None = None):
    """Async-generator query seam yielding one message with one text block."""
    async def gen(*, prompt, options):
        if calls is not None:
            calls.append(prompt)

        class _Block:
            text = reply_text

        class _Msg:
            content = [_Block()]
        yield _Msg()
    return gen


def _fenced(obj_json: str) -> str:
    return f"Sure, here you go:\n```json\n{obj_json}\n```"


# ---------------------------------------------------------------------------
# POST /api/dispatch/repo-check
# ---------------------------------------------------------------------------


def test_repo_check_returns_suggestion(isolated_db):
    client = _make_client()
    with client:
        with (
            patch("server.api.dispatch.recent_repo_vocabulary", new=AsyncMock(return_value=VOCAB)),
            patch(
                "server.api.dispatch.suggest_repo",
                new=AsyncMock(return_value=RepoSuggestion(
                    repo="cairnea/KinDash", reason="Task modal is a KinDash surface.")),
            ) as suggest,
        ):
            resp = client.post(
                "/api/dispatch/repo-check",
                json={"prompt": "Fix the task modal footer", "repo": "steiner385/viberpit"},
                headers=IDENTITY_HEADERS,
            )
    assert resp.status_code == 200
    assert resp.json() == {
        "suggested_repo": "cairnea/KinDash",
        "reason": "Task modal is a KinDash surface.",
    }
    # The endpoint hands the body's prompt + repo and the vocabulary through.
    args = suggest.await_args
    assert args.args[0] == "Fix the task modal footer"
    assert args.args[1] == "steiner385/viberpit"
    assert args.args[2] == VOCAB


def test_repo_check_none_yields_nulls(isolated_db):
    client = _make_client()
    with client:
        with (
            patch("server.api.dispatch.recent_repo_vocabulary", new=AsyncMock(return_value=VOCAB)),
            patch("server.api.dispatch.suggest_repo", new=AsyncMock(return_value=None)),
        ):
            resp = client.post(
                "/api/dispatch/repo-check",
                json={"prompt": "Fix the dispatch bar", "repo": "steiner385/viberpit"},
                headers=IDENTITY_HEADERS,
            )
    assert resp.status_code == 200
    assert resp.json() == {"suggested_repo": None, "reason": None}


def test_repo_check_empty_prompt_422(isolated_db):
    client = _make_client()
    with client:
        resp = client.post(
            "/api/dispatch/repo-check",
            json={"prompt": "", "repo": "steiner385/viberpit"},
            headers=IDENTITY_HEADERS,
        )
    assert resp.status_code == 422


def test_repo_check_accepts_null_repo(isolated_db):
    client = _make_client()
    with client:
        with (
            patch("server.api.dispatch.recent_repo_vocabulary", new=AsyncMock(return_value=VOCAB)),
            patch("server.api.dispatch.suggest_repo", new=AsyncMock(return_value=None)) as suggest,
        ):
            resp = client.post(
                "/api/dispatch/repo-check",
                json={"prompt": "Fix the dispatch bar", "repo": None},
                headers=IDENTITY_HEADERS,
            )
    assert resp.status_code == 200
    assert resp.json() == {"suggested_repo": None, "reason": None}
    assert suggest.await_args.args[1] is None


def test_repo_check_helper_exception_is_fail_open(isolated_db):
    """A blown-up helper never surfaces as a 500 — the endpoint answers nulls."""
    client = _make_client()
    with client:
        with (
            patch("server.api.dispatch.recent_repo_vocabulary", new=AsyncMock(return_value=VOCAB)),
            patch("server.api.dispatch.suggest_repo", new=AsyncMock(side_effect=RuntimeError("boom"))),
        ):
            resp = client.post(
                "/api/dispatch/repo-check",
                json={"prompt": "Fix the dispatch bar", "repo": "steiner385/viberpit"},
                headers=IDENTITY_HEADERS,
            )
    assert resp.status_code == 200
    assert resp.json() == {"suggested_repo": None, "reason": None}


# ---------------------------------------------------------------------------
# suggest_repo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suggest_high_confidence_other_repo(monkeypatch):
    monkeypatch.delenv("VIBERPIT_DISPATCH_REPO_CHECK_ENABLED", raising=False)
    reply = _fenced('{"repo": "cairnea/KinDash", "confidence": "high", '
                    '"reason": "Task modal lives in KinDash."}')
    got = await suggest_repo(
        "Fix the task modal footer", "steiner385/viberpit", VOCAB,
        _query_fn=_fake_query(reply), _config_dir="", _cwd="/tmp")
    assert got == RepoSuggestion(repo="cairnea/KinDash", reason="Task modal lives in KinDash.")


@pytest.mark.asyncio
async def test_suggest_prompt_lists_selected_candidates_and_draft():
    calls: list[str] = []
    reply = _fenced('{"repo": null, "confidence": "low", "reason": ""}')
    await suggest_repo(
        "Fix the task modal footer", "steiner385/viberpit", VOCAB,
        _query_fn=_fake_query(reply, calls=calls), _config_dir="", _cwd="/tmp")
    assert len(calls) == 1
    body = calls[0]
    assert "Selected repository: steiner385/viberpit" in body
    assert "- cairnea/KinDash\n    recent: Task modal footer | Roadmap tree" in body
    assert "- steiner385/viberpit\n    recent: Fix dispatch bar | Daemon drain" in body
    assert "```\nFix the task modal footer\n```" in body


@pytest.mark.asyncio
async def test_suggest_same_repo_as_selected_is_none():
    reply = _fenced('{"repo": "STEINER385/viberpit", "confidence": "high", "reason": "x"}')
    got = await suggest_repo(
        "Fix the dispatch bar", "steiner385/viberpit", VOCAB,
        _query_fn=_fake_query(reply), _config_dir="", _cwd="/tmp")
    assert got is None


@pytest.mark.asyncio
async def test_suggest_low_confidence_is_none():
    reply = _fenced('{"repo": "cairnea/KinDash", "confidence": "low", "reason": "maybe"}')
    got = await suggest_repo(
        "Fix the modal", "steiner385/viberpit", VOCAB,
        _query_fn=_fake_query(reply), _config_dir="", _cwd="/tmp")
    assert got is None


@pytest.mark.asyncio
async def test_suggest_repo_not_in_vocabulary_is_none():
    reply = _fenced('{"repo": "someone/else", "confidence": "high", "reason": "x"}')
    got = await suggest_repo(
        "Fix the modal", "steiner385/viberpit", VOCAB,
        _query_fn=_fake_query(reply), _config_dir="", _cwd="/tmp")
    assert got is None


@pytest.mark.asyncio
async def test_suggest_null_repo_is_none():
    reply = _fenced('{"repo": null, "confidence": "high", "reason": "fits selected"}')
    got = await suggest_repo(
        "Fix the dispatch bar", "steiner385/viberpit", VOCAB,
        _query_fn=_fake_query(reply), _config_dir="", _cwd="/tmp")
    assert got is None


@pytest.mark.asyncio
async def test_suggest_missing_reason_defaults_to_empty():
    reply = _fenced('{"repo": "cairnea/KinDash", "confidence": "high", "reason": 42}')
    got = await suggest_repo(
        "Fix the task modal", "steiner385/viberpit", VOCAB,
        _query_fn=_fake_query(reply), _config_dir="", _cwd="/tmp")
    assert got == RepoSuggestion(repo="cairnea/KinDash", reason="")


@pytest.mark.asyncio
async def test_suggest_malformed_json_is_none():
    got = await suggest_repo(
        "Fix the modal", "steiner385/viberpit", VOCAB,
        _query_fn=_fake_query("I think it is KinDash, not sure though"),
        _config_dir="", _cwd="/tmp")
    assert got is None


@pytest.mark.asyncio
async def test_suggest_stream_exception_is_none():
    async def boom(*, prompt, options):
        raise RuntimeError("model exploded")
        yield  # pragma: no cover — makes this an async generator

    got = await suggest_repo(
        "Fix the modal", "steiner385/viberpit", VOCAB,
        _query_fn=boom, _config_dir="", _cwd="/tmp")
    assert got is None


@pytest.mark.asyncio
async def test_suggest_disabled_never_calls_model(monkeypatch):
    monkeypatch.setenv("VIBERPIT_DISPATCH_REPO_CHECK_ENABLED", "false")
    assert repo_check_enabled() is False
    calls: list[str] = []
    reply = _fenced('{"repo": "cairnea/KinDash", "confidence": "high", "reason": "x"}')
    got = await suggest_repo(
        "Fix the task modal", "steiner385/viberpit", VOCAB,
        _query_fn=_fake_query(reply, calls=calls), _config_dir="", _cwd="/tmp")
    assert got is None
    assert calls == []


@pytest.mark.asyncio
async def test_suggest_blank_prompt_never_calls_model():
    calls: list[str] = []
    got = await suggest_repo(
        "   ", "steiner385/viberpit", VOCAB,
        _query_fn=_fake_query("{}", calls=calls), _config_dir="", _cwd="/tmp")
    assert got is None
    assert calls == []


@pytest.mark.asyncio
async def test_suggest_empty_vocabulary_never_calls_model():
    calls: list[str] = []
    got = await suggest_repo(
        "Fix the modal", "steiner385/viberpit", [],
        _query_fn=_fake_query("{}", calls=calls), _config_dir="", _cwd="/tmp")
    assert got is None
    assert calls == []


@pytest.mark.asyncio
async def test_suggest_only_selected_repo_in_vocabulary_never_calls_model():
    calls: list[str] = []
    got = await suggest_repo(
        "Fix the modal", "steiner385/viberpit", [VOCAB[0]],
        _query_fn=_fake_query("{}", calls=calls), _config_dir="", _cwd="/tmp")
    assert got is None
    assert calls == []


@pytest.mark.asyncio
async def test_suggest_no_selected_repo_with_high_confidence_pick():
    reply = _fenced('{"repo": "cairnea/KinDash", "confidence": "high", "reason": "Modal work."}')
    got = await suggest_repo(
        "Fix the task modal", None, VOCAB,
        _query_fn=_fake_query(reply), _config_dir="", _cwd="/tmp")
    assert got == RepoSuggestion(repo="cairnea/KinDash", reason="Modal work.")


@pytest.mark.asyncio
async def test_suggest_caps_prompt_length():
    calls: list[str] = []
    reply = _fenced('{"repo": null, "confidence": "low", "reason": ""}')
    await suggest_repo(
        "x" * 10_000, "steiner385/viberpit", VOCAB,
        _query_fn=_fake_query(reply, calls=calls), _config_dir="", _cwd="/tmp")
    assert "x" * 4000 in calls[0]
    assert "x" * 4001 not in calls[0]


def test_repo_check_enabled_parses_truthy_values(monkeypatch):
    monkeypatch.delenv("VIBERPIT_DISPATCH_REPO_CHECK_ENABLED", raising=False)
    assert repo_check_enabled() is True
    for value in ("1", "true", "YES", "on"):
        monkeypatch.setenv("VIBERPIT_DISPATCH_REPO_CHECK_ENABLED", value)
        assert repo_check_enabled() is True
    for value in ("0", "false", "off", "nope"):
        monkeypatch.setenv("VIBERPIT_DISPATCH_REPO_CHECK_ENABLED", value)
        assert repo_check_enabled() is False


# ---------------------------------------------------------------------------
# recent_repo_vocabulary
# ---------------------------------------------------------------------------


def _row(id: str, repo: str | None, *, age: timedelta, title: str | None = None,
         initial_prompt: str | None = None, kind: str | None = None) -> DbSession:
    return DbSession(
        id=id, name=id, account_name="acct", status="idle", cwd="/tmp/" + id,
        repo=repo, title=title, initial_prompt=initial_prompt, kind=kind,
        created_at=datetime.utcnow() - age,
    )


@pytest.mark.asyncio
async def test_recent_repo_vocabulary_groups_orders_dedupes_and_falls_back(isolated_db):
    async with db_session() as db:
        db.add_all([
            # viberpit: newest session overall → first repo; titles newest first.
            _row("v1", "steiner385/viberpit", age=timedelta(hours=1), title="Dispatch bar"),
            _row("v2", "steiner385/viberpit", age=timedelta(days=2), title="Daemon drain"),
            # duplicate title → collapsed onto the newer occurrence
            _row("v3", "steiner385/viberpit", age=timedelta(days=3), title="Dispatch bar"),
            # empty title → first 80 chars of initial_prompt
            _row("v4", "steiner385/viberpit", age=timedelta(days=4), title="",
                 initial_prompt="p" * 100),
            # KinDash: newest session is older than viberpit's → second repo
            _row("k1", "cairnea/KinDash", age=timedelta(days=5), title="Task modal"),
            _row("k2", "cairnea/KinDash", age=timedelta(days=6), title="Roadmap tree"),
            # excluded: assistant kind, no repo, empty repo, outside the window
            _row("a1", "cairnea/KinDash", age=timedelta(hours=2), title="Assistant chat",
                 kind="assistant"),
            _row("n1", None, age=timedelta(hours=2), title="Scratch"),
            _row("e1", "", age=timedelta(hours=2), title="Scratch"),
            _row("o1", "old/repo", age=timedelta(days=200), title="Ancient"),
            # no title AND no prompt → contributes nothing but still registers the repo
            _row("b1", "bare/repo", age=timedelta(days=7)),
        ])

    vocab = await recent_repo_vocabulary()

    assert [v.repo for v in vocab] == ["steiner385/viberpit", "cairnea/KinDash", "bare/repo"]
    assert vocab[0].titles == ["Dispatch bar", "Daemon drain", "p" * 80]
    assert vocab[1].titles == ["Task modal", "Roadmap tree"]
    assert vocab[2].titles == []


@pytest.mark.asyncio
async def test_recent_repo_vocabulary_caps_titles_and_repos(isolated_db):
    async with db_session() as db:
        rows = []
        for r in range(4):
            for t in range(5):
                rows.append(_row(f"r{r}t{t}", f"owner/repo{r}",
                                 age=timedelta(days=r, hours=t), title=f"title-{r}-{t}"))
        db.add_all(rows)

    vocab = await recent_repo_vocabulary(titles_per_repo=2, max_repos=3)

    assert [v.repo for v in vocab] == ["owner/repo0", "owner/repo1", "owner/repo2"]
    assert all(v.titles == [f"title-{i}-0", f"title-{i}-1"] for i, v in enumerate(vocab))


@pytest.mark.asyncio
async def test_recent_repo_vocabulary_window(isolated_db):
    async with db_session() as db:
        db.add_all([
            _row("in", "owner/in", age=timedelta(days=3), title="Inside"),
            _row("out", "owner/out", age=timedelta(days=10), title="Outside"),
        ])
    vocab = await recent_repo_vocabulary(days=7)
    assert [v.repo for v in vocab] == ["owner/in"]


@pytest.mark.asyncio
async def test_recent_repo_vocabulary_db_failure_is_empty():
    with patch("server.sessions.repo_match.db_session", side_effect=RuntimeError("no db")):
        assert await recent_repo_vocabulary() == []


@pytest.mark.asyncio
async def test_suggest_org_denied_logs_the_cause_and_bars_the_account(caplog):
    """#1849: suggest_repo logged "model call failed: Claude Code returned an
    error result: success" six times with no cause. The log now carries the
    classified kind and the CLI's own sentence, and the barred account is not
    called again — the next dispatch fails over (or degrades) instead."""
    import logging

    from server.sessions.helper_call import reset_breakers
    from server.tests.fakes.helper_streams import org_denied_query

    reset_breakers()
    calls: list = []
    with caplog.at_level(logging.WARNING, logger="server.sessions.repo_match"):
        for _ in range(2):
            assert await suggest_repo(
                "Fix the modal", "steiner385/viberpit", VOCAB,
                _query_fn=org_denied_query(calls), _config_dir="/cfg/org-denied",
                _cwd="/tmp") is None
    failed = [r.getMessage() for r in caplog.records
              if r.name == "server.sessions.repo_match"]
    assert len(failed) == 1
    assert "kind=auth_forbidden" in failed[0]
    assert "organization has disabled" in failed[0]
    assert len(calls) == 1
