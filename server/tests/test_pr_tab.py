"""D-15 PR tab, server half: review threads in the PR detail, merge-now and
disable-auto-merge, and the ``hold:human`` guard on every REST merge/arm path.

``gh`` is faked at the orchestrator's ``_gh_run`` seam (as in test_ready_merge.py /
test_pipeline_pr_detail.py); route tests swap ``app.state.sessions`` for a fake.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from server.github.pr_status import (
    PR_HUMAN_HOLD,
    PR_HUMAN_HOLD_UNKNOWN,
    PipelinePr,
    PullRequestInfo,
    build_pr_detail_query,
    parse_pr_detail_response,
)
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_H = {"Tailscale-User-Login": "testuser@example.com"}

_ENV = {
    "VIBERPIT_DB_URL": "sqlite+aiosqlite:///:memory:",
    "VIBERPIT_DEV": "1",
    "VIBERPIT_TAILNET_ALLOWLIST": "",
}


def _orch() -> SessionOrchestrator:
    with patch.dict(os.environ, _ENV):
        return SessionOrchestrator(
            sdk_factory=fake_sdk_factory(), poll_interval=9999.0, usage_fn=lambda name: None)


def _ok(args, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr=stderr)


def _labels_json(*names):
    return json.dumps({"labels": [{"id": f"L{i}", "name": n} for i, n in enumerate(names)]})


class _Gh:
    """Fake ``_gh_run``: answers the live label read with *labels* (or *label_fail*),
    and every other call with *merge_rc*/*merge_stderr*. Records every argv."""

    def __init__(self, labels=(), *, label_fail=None, label_raises=None,
                 merge_rc=0, merge_stderr=""):
        self.labels = labels
        self.label_fail = label_fail
        self.label_raises = label_raises
        self.merge_rc = merge_rc
        self.merge_stderr = merge_stderr
        self.calls: list[list[str]] = []
        self.tokens: list[object] = []

    def __call__(self, args, token=None, *, input=None, timeout=None):
        self.calls.append(list(args))
        self.tokens.append(token)
        if args[:2] == ["pr", "view"]:
            if self.label_raises is not None:
                raise self.label_raises
            if self.label_fail is not None:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr=self.label_fail)
            return _ok(args, stdout=_labels_json(*self.labels))
        return subprocess.CompletedProcess(args, self.merge_rc, stdout="",
                                           stderr=self.merge_stderr)

    def mutations(self):
        return [c for c in self.calls if c[:2] != ["pr", "view"]]


# --------------------------------------------------------------------------- #
# Parser: review threads                                                      #
# --------------------------------------------------------------------------- #


def test_detail_query_fetches_review_threads_with_bodies():
    q = build_pr_detail_query("o/r", 7)
    assert "reviewThreads(first: 50)" in q
    for field in ("id", "isResolved", "isOutdated", "path", "line", "originalLine"):
        assert field in q
    assert "comments(first: 20)" in q
    for field in ("author { login }", "body", "createdAt", "url"):
        assert field in q


def _thread(**kw):
    base = {"id": "T1", "isResolved": False, "isOutdated": False, "path": "a.py",
            "line": 3, "originalLine": 3,
            "comments": {"nodes": [{"author": {"login": "alice"}, "body": "nit",
                                    "createdAt": "2026-11-01T00:00:00Z",
                                    "url": "https://gh/c1"}]}}
    base.update(kw)
    return base


def test_parser_returns_threads_in_api_order_and_drops_malformed():
    resp = {"data": {"repository": {"pullRequest": {
        "mergeable": "MERGEABLE", "reviews": {"nodes": []}, "commits": {"nodes": []},
        "reviewThreads": {"nodes": [
            _thread(id="T1"),
            "not-a-dict",
            _thread(id="T2", path=None),          # no path → dropped
            _thread(id=None),                      # no id → dropped
            _thread(id="T3", isResolved=True, isOutdated=True, line=None, originalLine=9,
                    comments={"nodes": [None, {"author": None, "body": "**b**",
                                               "createdAt": "x", "url": "u"}]}),
        ]},
    }}}}
    out = parse_pr_detail_response(resp)
    assert out["threads"] == [
        {"id": "T1", "path": "a.py", "line": 3, "resolved": False, "outdated": False,
         "comments": [{"author": "alice", "body": "nit",
                       "createdAt": "2026-11-01T00:00:00Z", "url": "https://gh/c1"}]},
        # outdated: GitHub nulls `line`; fall back to originalLine. A null comment drops,
        # a ghost author reads as None.
        {"id": "T3", "path": "a.py", "line": 9, "resolved": True, "outdated": True,
         "comments": [{"author": None, "body": "**b**", "createdAt": "x", "url": "u"}]},
    ]
    assert out["mergeable"] == "MERGEABLE"


def test_parser_empty_shape_carries_threads():
    assert parse_pr_detail_response({}) == {
        "checks": [], "reviews": [], "mergeable": None, "threads": []}


# --------------------------------------------------------------------------- #
# orch.pr_detail: new fields + prs_for fallback                               #
# --------------------------------------------------------------------------- #


def _detail_gh(threads=()):
    def fake_gh(args, token=None, *, input=None, timeout=None):
        payload = {"data": {"repository": {"pullRequest": {
            "mergeable": "MERGEABLE", "reviews": {"nodes": []}, "commits": {"nodes": []},
            "reviewThreads": {"nodes": list(threads)}}}}}
        return _ok(args, stdout=json.dumps(payload))
    return fake_gh


@pytest.mark.asyncio
async def test_pr_detail_returns_threads_and_cached_flags():
    orch = _orch()
    info = PullRequestInfo(number=42, url="u", state="draft", checks="passing", title="t",
                           repo="o/r", is_draft=True, auto_merge_enabled=True,
                           merge_state_status="BLOCKED", labels=("hold:human", "x"))
    orch.all_prs = AsyncMock(return_value=[PipelinePr(pr=info, session_id="s1",
                                                      session_name="S")])
    orch._gh_run = _detail_gh([_thread(id="T1")])
    out = await orch.pr_detail("o/r", 42)
    assert [t["id"] for t in out["threads"]] == ["T1"]
    assert out["isDraft"] is True
    assert out["autoMergeEnabled"] is True
    assert out["mergeStateStatus"] == "BLOCKED"
    assert out["labels"] == ["hold:human", "x"]
    assert out["humanHold"] is True
    assert out["sessionId"] == "s1"


@pytest.mark.asyncio
async def test_pr_detail_falls_back_to_session_cache_on_population_miss():
    """Review Focus 4: a session PR on a repo outside the Pipeline population."""
    orch = _orch()
    orch.all_prs = AsyncMock(return_value=[])
    orch._records["s1"] = SimpleNamespace(id="s1", repo="o/side", name="Side quest")
    orch._records["s2"] = SimpleNamespace(id="s2", repo="o/other", name="Other")
    info = PullRequestInfo(number=5, url="https://gh/o/side/pull/5", state="open",
                           checks="failing", title="Side PR", merge_state_status="CLEAN")
    orch.prs_for = lambda sid: [info] if sid == "s1" else []
    orch._gh_run = _detail_gh()
    out = await orch.pr_detail("o/side", 5)
    assert out is not None
    assert out["number"] == 5 and out["title"] == "Side PR"
    assert out["repo"] == "o/side"
    assert out["sessionId"] == "s1"
    assert out["session"] == {"id": "s1", "name": "Side quest"}
    assert out["mergeStateStatus"] == "CLEAN"
    assert out["humanHold"] is False
    assert out["threads"] == []
    # Same repo, unknown number: still a plain not-found.
    assert await orch.pr_detail("o/side", 6) is None


@pytest.mark.asyncio
async def test_pr_detail_fallback_beats_the_sync_error_marker():
    orch = _orch()
    orch.all_prs = AsyncMock(return_value=[])
    orch._pr_population._last_error["o/side"] = "gh: HTTP 504"
    orch._records["s1"] = SimpleNamespace(id="s1", repo="o/side", name="S")
    info = PullRequestInfo(number=5, url="u", state="open", checks="none", title="t")
    orch.prs_for = lambda sid: [info]
    orch._gh_run = _detail_gh()
    out = await orch.pr_detail("o/side", 5)
    assert out["number"] == 5 and "pr_not_synced" not in out


# --------------------------------------------------------------------------- #
# orch mutations                                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_merge_pr_squash_merges_after_a_live_label_read():
    orch = _orch()
    gh = _Gh(labels=("bug",))
    orch._gh_run = gh
    assert await orch.merge_pr("o/r", 7) == {"ok": True, "error": ""}
    assert gh.calls[0][:5] == ["pr", "view", "7", "-R", "o/r"]
    assert "labels" in gh.calls[0]
    assert gh.mutations() == [["pr", "merge", "7", "-R", "o/r", "--squash"]]


@pytest.mark.asyncio
async def test_merge_pr_failure_returns_gh_stderr_verbatim_at_info(caplog):
    orch = _orch()
    orch._gh_run = _Gh(merge_rc=1, merge_stderr="  Pull request is not mergeable\n")
    with caplog.at_level(logging.DEBUG, logger="server.sessions.orchestrator"):
        out = await orch.merge_pr("o/r", 7)
    assert out == {"ok": False, "error": "Pull request is not mergeable"}
    mine = [r for r in caplog.records if "pr.merge" in r.getMessage()]
    assert mine and all(r.levelno == logging.INFO for r in mine)


@pytest.mark.asyncio
async def test_disable_auto_merge_runs_disable_auto_even_when_held():
    orch = _orch()
    gh = _Gh(labels=("hold:human",))
    orch._gh_run = gh
    assert await orch.disable_auto_merge("o/r", 7) == {"ok": True, "error": ""}
    assert gh.calls == [["pr", "merge", "7", "-R", "o/r", "--disable-auto"]]


@pytest.mark.asyncio
async def test_disable_auto_merge_failure_returns_stderr():
    orch = _orch()
    orch._gh_run = _Gh(merge_rc=1, merge_stderr="not enabled")
    assert await orch.disable_auto_merge("o/r", 7) == {"ok": False, "error": "not enabled"}


@pytest.mark.asyncio
async def test_held_pr_refused_by_merge_and_ready_merge_from_the_live_read():
    """Review Focus 1: no cache knows about the label — only the live read does."""
    orch = _orch()
    gh = _Gh(labels=("Hold:Human",))
    orch._gh_run = gh
    merged = await orch.merge_pr("o/r", 7)
    assert merged["ok"] is False and merged["code"] == PR_HUMAN_HOLD
    assert "hold:human" in merged["error"]
    armed = await orch.ready_merge("o/r", 7)
    assert armed["code"] == PR_HUMAN_HOLD
    assert armed["autoMergeArmed"] is False and armed["markedReady"] is False
    assert gh.mutations() == []


@pytest.mark.asyncio
async def test_unknown_hold_state_fails_closed_with_gh_text():
    orch = _orch()
    gh = _Gh(label_fail="HTTP 502: Bad Gateway")
    orch._gh_run = gh
    merged = await orch.merge_pr("o/r", 7)
    assert merged == {"ok": False, "code": PR_HUMAN_HOLD_UNKNOWN,
                      "error": "HTTP 502: Bad Gateway"}
    armed = await orch.ready_merge("o/r", 7)
    assert armed["code"] == PR_HUMAN_HOLD_UNKNOWN
    assert armed["error"] == "HTTP 502: Bad Gateway"
    assert gh.mutations() == []


@pytest.mark.asyncio
async def test_label_read_timeout_fails_closed():
    orch = _orch()
    gh = _Gh(label_raises=subprocess.TimeoutExpired(["gh"], 15))
    orch._gh_run = gh
    merged = await orch.merge_pr("o/r", 7)
    assert merged["ok"] is False and merged["code"] == PR_HUMAN_HOLD_UNKNOWN
    assert merged["error"]
    assert gh.mutations() == []


@pytest.mark.asyncio
async def test_label_read_unparseable_fails_closed():
    orch = _orch()

    def fake_gh(args, token=None, *, input=None, timeout=None):
        return _ok(args, stdout="not json")

    orch._gh_run = fake_gh
    assert (await orch.merge_pr("o/r", 7))["code"] == PR_HUMAN_HOLD_UNKNOWN


# --------------------------------------------------------------------------- #
# Routes                                                                       #
# --------------------------------------------------------------------------- #


class _FakeSessions:
    def __init__(self):
        self.calls: list[tuple] = []
        self.merge_result: dict = {"ok": True, "error": ""}
        self.disable_result: dict = {"ok": True, "error": ""}
        self.ready_result: dict = {"markedReady": True, "autoMergeArmed": True,
                                   "alreadyArmed": False, "cleanReadyToMerge": False,
                                   "error": ""}

    async def merge_pr(self, repo, number):
        self.calls.append(("merge_pr", repo, number))
        return self.merge_result

    async def disable_auto_merge(self, repo, number):
        self.calls.append(("disable_auto_merge", repo, number))
        return self.disable_result

    async def ready_merge(self, repo, number):
        self.calls.append(("ready_merge", repo, number))
        return self.ready_result


def _client(tmp_path, monkeypatch):
    from server.app import create_app
    monkeypatch.setenv("VIBERPIT_DB_URL", f"sqlite+aiosqlite:///{tmp_path / 'vp.db'}")
    monkeypatch.setenv("VIBERPIT_DEV", "1")
    monkeypatch.setenv("VIBERPIT_TAILNET_ALLOWLIST", "")
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                               usage_fn=lambda name: None)
    app = create_app(orchestrator=orch)
    fake = _FakeSessions()
    app.state.sessions = fake
    return TestClient(app, raise_server_exceptions=False), fake


def _audit_rows(client, type_):
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import AuditEvent

    async def _q():
        async with db_session() as db:
            return (await db.execute(
                select(AuditEvent).where(AuditEvent.type == type_))).scalars().all()

    assert client.portal is not None
    return client.portal.call(_q)


def test_merge_route_squash_merges_and_audits(tmp_path, monkeypatch):
    client, fake = _client(tmp_path, monkeypatch)
    with client:
        r = client.post("/api/prs/acme/billing/42/merge", json={"method": "squash"},
                        headers=_H)
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "error": ""}
        rows = _audit_rows(client, "pr.merge")
    assert fake.calls == [("merge_pr", "acme/billing", 42)]
    assert len(rows) == 1
    assert rows[0].identity == "testuser@example.com"
    assert rows[0].detail["repo"] == "acme/billing"
    assert rows[0].detail["number"] == 42
    assert rows[0].detail["ok"] is True


def test_merge_route_rejects_non_squash_methods(tmp_path, monkeypatch):
    client, fake = _client(tmp_path, monkeypatch)
    with client:
        for method in ("merge", "rebase", ""):
            r = client.post("/api/prs/acme/billing/42/merge", json={"method": method},
                            headers=_H)
            assert r.status_code == 422, (method, r.text)
    assert fake.calls == []


def test_merge_route_requires_identity(tmp_path, monkeypatch):
    client, fake = _client(tmp_path, monkeypatch)
    with client:
        r = client.post("/api/prs/acme/billing/42/merge", json={"method": "squash"})
    assert r.status_code == 401
    assert fake.calls == []


def test_merge_route_gh_failure_is_200_with_stderr(tmp_path, monkeypatch):
    client, fake = _client(tmp_path, monkeypatch)
    fake.merge_result = {"ok": False, "error": "Pull request is not mergeable"}
    with client:
        r = client.post("/api/prs/acme/billing/42/merge", json={"method": "squash"},
                        headers=_H)
    assert r.status_code == 200
    assert r.json() == {"ok": False, "error": "Pull request is not mergeable"}


@pytest.mark.parametrize("code", [PR_HUMAN_HOLD, PR_HUMAN_HOLD_UNKNOWN])
def test_merge_route_held_is_409_and_still_audited(tmp_path, monkeypatch, code):
    client, fake = _client(tmp_path, monkeypatch)
    fake.merge_result = {"ok": False, "code": code, "error": "why"}
    with client:
        r = client.post("/api/prs/acme/billing/42/merge", json={"method": "squash"},
                        headers=_H)
        rows = _audit_rows(client, "pr.merge")
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": code, "error": "why"}
    assert len(rows) == 1 and rows[0].detail["code"] == code


def test_auto_merge_disable_route(tmp_path, monkeypatch):
    client, fake = _client(tmp_path, monkeypatch)
    with client:
        r = client.post("/api/prs/acme/billing/42/auto-merge", json={"enabled": False},
                        headers=_H)
        rows = _audit_rows(client, "pr.auto_merge_disabled")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "error": ""}
    assert fake.calls == [("disable_auto_merge", "acme/billing", 42)]
    assert len(rows) == 1 and rows[0].detail["number"] == 42


def test_auto_merge_enable_reuses_ready_merge(tmp_path, monkeypatch):
    client, fake = _client(tmp_path, monkeypatch)
    with client:
        r = client.post("/api/prs/acme/billing/42/auto-merge", json={"enabled": True},
                        headers=_H)
        rows = _audit_rows(client, "pr.ready_merge")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["error"] == "" and body["autoMergeArmed"] is True
    assert fake.calls == [("ready_merge", "acme/billing", 42)]
    assert len(rows) == 1


def test_auto_merge_enable_on_held_pr_is_409(tmp_path, monkeypatch):
    client, fake = _client(tmp_path, monkeypatch)
    fake.ready_result = {"markedReady": False, "autoMergeArmed": False,
                         "alreadyArmed": False, "cleanReadyToMerge": False,
                         "code": PR_HUMAN_HOLD, "error": "held"}
    with client:
        r = client.post("/api/prs/acme/billing/42/auto-merge", json={"enabled": True},
                        headers=_H)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == PR_HUMAN_HOLD


def test_auto_merge_requires_enabled(tmp_path, monkeypatch):
    client, fake = _client(tmp_path, monkeypatch)
    with client:
        r = client.post("/api/prs/acme/billing/42/auto-merge", json={}, headers=_H)
    assert r.status_code == 422
    assert fake.calls == []


def test_pipeline_ready_merge_route_refuses_held_pr_and_audits(tmp_path, monkeypatch):
    """Review Focus 2: the Pipeline page's ready-merge route gains the guard too."""
    client, fake = _client(tmp_path, monkeypatch)
    fake.ready_result = {"markedReady": False, "autoMergeArmed": False,
                         "alreadyArmed": False, "cleanReadyToMerge": False,
                         "code": PR_HUMAN_HOLD, "error": "o/r#7 carries hold:human"}
    with client:
        r = client.post("/api/pipeline/pr/ready-merge", json={"repo": "o/r", "number": 7},
                        headers=_H)
        rows = _audit_rows(client, "pr.ready_merge")
    assert r.status_code == 409
    assert r.json()["detail"] == {"code": PR_HUMAN_HOLD, "error": "o/r#7 carries hold:human"}
    assert len(rows) == 1
    assert rows[0].detail["repo"] == "o/r" and rows[0].detail["number"] == 7


def test_pipeline_ready_merge_route_unheld_is_unchanged(tmp_path, monkeypatch):
    client, fake = _client(tmp_path, monkeypatch)
    with client:
        r = client.post("/api/pipeline/pr/ready-merge", json={"repo": "o/r", "number": 7},
                        headers=_H)
        rows = _audit_rows(client, "pr.ready_merge")
    assert r.status_code == 200
    assert r.json() == fake.ready_result
    assert len(rows) == 1 and rows[0].detail["ok"] is True


# --------------------------------------------------------------------------- #
# Assistant policy: the new mutation routes are NOT reachable by its token     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", ["/api/prs/acme/billing/42/merge",
                                  "/api/prs/acme/billing/42/auto-merge"])
@pytest.mark.parametrize("operator", [False, True])
def test_new_mutation_routes_absent_from_assistant_allowlist(path, operator):
    from server.assistant.policy import route_allowed
    assert route_allowed("POST", path, operator=operator) is False


# --------------------------------------------------------------------------- #
# Remote mode: facade → RemoteOrchestrator → daemon RPC                        #
# --------------------------------------------------------------------------- #


class _RecordingClient:
    def __init__(self, return_value):
        self.return_value = return_value
        self.last_call = None

    async def call(self, method, *, timeout=None, **kwargs):
        self.last_call = (method, kwargs)
        return self.return_value


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["merge_pr", "disable_auto_merge"])
async def test_remote_orchestrator_forwards_mutations(method):
    from server.sessions.remote import RemoteOrchestrator
    client = _RecordingClient({"ok": True, "error": ""})
    remote = RemoteOrchestrator(client)
    assert await getattr(remote, method)("o/r", 7) == {"ok": True, "error": ""}
    assert client.last_call == (method, {"repo": "o/r", "number": 7})


@pytest.mark.parametrize("method", ["merge_pr", "disable_auto_merge"])
def test_daemon_rpc_registers_mutations(method):
    from server.ipc.daemon import create_daemon_app
    from server.ipc.orchestrator_rpc import register_orchestrator_rpc
    app = create_daemon_app()
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda n: None)
    register_orchestrator_rpc(app, orch)
    setattr(orch, method, AsyncMock(return_value={"ok": False, "code": PR_HUMAN_HOLD,
                                                  "error": "held"}))
    with TestClient(app) as c:
        r = c.post(f"/rpc/{method}", json={"repo": "o/r", "number": 7}).json()
    assert r["result"] == {"ok": False, "code": PR_HUMAN_HOLD, "error": "held"}
    getattr(orch, method).assert_awaited_once_with("o/r", 7)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["merge_pr", "disable_auto_merge"])
async def test_facade_delegates_mutations(method):
    from server.sessions.facade import InProcessOrchestratorAdapter
    orch = mock.MagicMock()
    setattr(orch, method, AsyncMock(return_value={"ok": True, "error": ""}))
    f = InProcessOrchestratorAdapter(orch)
    assert await getattr(f, method)("o/r", 7) == {"ok": True, "error": ""}
    getattr(orch, method).assert_awaited_once_with("o/r", 7)
