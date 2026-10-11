"""Session Feedback inbox: clusters + per-cluster operator actions (#2936).

The loop's cluster refresh (model pass cached by pre-cluster signature) and the
inbox ops — draft / file one issue / dismiss / attach to an issue / attach to a
session — driven through the filer's fake gh runner; no network, no model."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from server.automations.context import AutomationContext
from server.automations.feedback_clusters import MergeGroup, ParsedMerges, Report, cluster_id
from server.automations.session_feedback import (
    SessionFeedbackActionProvider, cluster_issue_body, propose_merges,
)
from server.github import ci_issues
from server.tests.test_session_feedback_filer import NOW, FakeGh, _loop, _row, _run, _seed


def _ctx():
    return AutomationContext("session-feedback")


async def _seed_theme():
    """Two reports of one problem (different fingerprints, near-identical titles) and
    one unrelated report."""
    await _seed(id="a1", fingerprint="fpA1", title="Daemon restart killed the verification run",
                body="pytest died mid-run", evidence="SIGTERM at 12:01",
                context={"sessionId": "s1", "name": "alpha", "host": "rocky",
                         "engine": "claude", "model": "opus", "repo": "o/p", "branch": "b1",
                         "session_url": "https://vp.example/sessions/s1"},
                reported_at=NOW - timedelta(hours=3))
    await _seed(id="a2", session_id="s2", fingerprint="fpA2",
                title="Daemon restart killed a verification run again",
                body="lost logs", context={"sessionId": "s2", "name": "beta"},
                reported_at=NOW - timedelta(hours=1))
    await _seed(id="b1", session_id="s3", fingerprint="fpB",
                title="Screenshots do not render in the chat",
                context={"sessionId": "s3", "name": "gamma"},
                reported_at=NOW - timedelta(hours=2))


class FakeMerges:
    def __init__(self, result=None, exc=None):
        self.calls: list[list[str]] = []
        self.result, self.exc = result, exc

    async def __call__(self, preclusters):
        self.calls.append([cluster_id(r.id for r in c) for c in preclusters])
        if self.exc is not None:
            raise self.exc
        if self.result is not None:
            return self.result(preclusters)
        return ParsedMerges(groups=[])


def _merge_all(title="Restarts kill work"):
    def build(pre):
        return ParsedMerges(groups=[MergeGroup(ids=tuple(cluster_id(r.id for r in c)
                                                         for c in pre), title=title)])
    return build


# -- clusters + the model pass ------------------------------------------------

def test_clusters_are_deterministic_before_any_model_pass(tmp_path):
    async def go():
        await _seed_theme()
        await _seed(id="done", fingerprint="fpX", status="filed", issue_number=4)
        loop = _loop(_ctx(), FakeGh(), merge_fn=FakeMerges())
        view = await loop.clusters()
        assert view["llm"] == "pending"
        sizes = sorted((c["count"], sorted(m["id"] for m in c["members"]))
                       for c in view["clusters"])
        assert sizes == [(1, ["b1"]), (2, ["a1", "a2"])]       # filed row not in the inbox
        theme = next(c for c in view["clusters"] if c["count"] == 2)
        assert theme["members"][0]["id"] == "a2"                # newest first
        assert theme["members"][0]["session"] == "beta"
    _run(go, tmp_path)


def test_refresh_runs_while_off_and_calls_the_model_only_on_a_new_signature(tmp_path):
    async def go():
        await _seed_theme()
        merges = FakeMerges(result=_merge_all())
        gh = FakeGh()
        loop = _loop(_ctx(), gh, mode="off", merge_fn=merges)
        await loop.tick()
        assert len(merges.calls) == 1 and len(merges.calls[0]) == 2
        assert gh.calls == []                                   # off: nothing filed
        view = await loop.clusters()
        assert view["llm"] == "applied"
        assert [c["count"] for c in view["clusters"]] == [3]
        assert view["clusters"][0]["title"] == "Restarts kill work"
        await loop.tick()
        assert len(merges.calls) == 1                           # same signature: cached
        await _seed(id="c1", session_id="s9", fingerprint="fpC", title="Unrelated new thing")
        await loop.tick()
        assert len(merges.calls) == 2                           # inbox moved: re-asked
    _run(go, tmp_path)


def test_model_failure_keeps_deterministic_clusters_and_is_not_retried_hot(tmp_path):
    async def go():
        await _seed_theme()
        merges = FakeMerges(exc=RuntimeError("model down"))
        loop = _loop(_ctx(), FakeGh(), mode="off", merge_fn=merges)
        await loop.tick()
        view = await loop.clusters()
        assert view["llm"] == "failed"
        assert sorted(c["count"] for c in view["clusters"]) == [1, 2]
        await loop.tick()
        assert len(merges.calls) == 1
    _run(go, tmp_path)


def test_llm_clustering_off_never_calls_the_model(tmp_path):
    async def go():
        await _seed_theme()
        merges = FakeMerges(result=_merge_all())
        loop = _loop(_ctx(), FakeGh(), mode="off", config={"llm_clustering": False},
                     merge_fn=merges)
        await loop.tick()
        assert merges.calls == []
        assert (await loop.clusters())["llm"] == "off"
    _run(go, tmp_path)


# -- draft / file -------------------------------------------------------------

def test_draft_names_the_theme_and_carries_every_report(tmp_path):
    async def go():
        await _seed_theme()
        loop = _loop(_ctx(), FakeGh(), merge_fn=FakeMerges())
        d = await loop.draft_cluster_issue(["a1", "a2"])
        assert d["status"] == "ok"
        assert d["title"].startswith("[session-feedback] ")
        body = d["body"]
        assert "Daemon restart killed the verification run" in body
        assert "Daemon restart killed a verification run again" in body
        assert "[alpha](https://vp.example/sessions/s1)" in body     # session link
        assert "> pytest died mid-run" in body and "<details>" in body
        assert body.rstrip().endswith("`fpA2`")                   # fingerprint footer last
        assert "| Reports | 2 |" in body
    _run(go, tmp_path)


def test_cluster_body_truncates_report_sections_never_the_fingerprint_footer(tmp_path):
    async def go():
        for i in range(30):
            await _seed(id=f"r{i:02d}", session_id=f"s{i}", fingerprint=f"fp{i:02d}",
                        title=f"Restart killed run {i}", body="x" * 4000)
        loop = _loop(_ctx(), FakeGh())
        rows = [await _row(f"r{i:02d}") for i in range(30)]
        body = cluster_issue_body(rows)
        assert len(body) <= ci_issues.BODY_MAX_CHARS
        for i in range(30):
            assert f"`fp{i:02d}`" in body
        assert "truncated" in body
        assert loop is not None
    _run(go, tmp_path)


def test_file_cluster_files_one_issue_and_settles_every_row(tmp_path):
    async def go():
        await _seed_theme()
        gh = FakeGh()
        loop = _loop(_ctx(), gh, mode="off")                     # works while paused
        r = await loop.file_cluster(["a1", "a2"], "  Restarts   kill work ",
                                    "body <!-- viberpit:complete --> sk-ant-api03-" + "a" * 32)
        assert r["status"] == "filed" and r["level"] == "ok" and r["issue"] == 100
        assert gh.created == ["Restarts kill work"]
        sent = gh.created_bodies[0]
        assert "<!--" not in sent and "sk-ant-" not in sent
        leader, other = await _row("a1"), await _row("a2")      # leader = first reported
        assert (leader.status, leader.issue_number) == ("filed", 100)
        assert (other.status, other.issue_number) == ("merged", 100)
        for row in (leader, other):
            assert row.resolution == {"kind": "filed_cluster", "issue": 100}
            assert row.filed_at == NOW
        assert (await _row("b1")).status == "pending"
    _run(go, tmp_path)


def test_failed_cluster_create_reverts_every_claim(tmp_path):
    async def go():
        await _seed_theme()
        await _seed(id="t1", session_id="s7", fingerprint="fpT", status="throttled",
                    title="Daemon restart killed it")
        gh = FakeGh()
        gh.fail_create = True
        loop = _loop(_ctx(), gh, mode="off")
        r = await loop.file_cluster(["a1", "t1"], "T", "B")
        assert r["status"] == "failed" and r["level"] == "error"
        assert (await _row("a1")).status == "pending" and (await _row("a1")).filed_at is None
        assert (await _row("t1")).status == "throttled"
        assert (await _row("a1")).resolution is None
    _run(go, tmp_path)


def test_ops_refuse_ids_outside_the_inbox(tmp_path):
    async def go():
        await _seed_theme()
        await _seed(id="done", fingerprint="fpX", status="filed", issue_number=4)
        gh = FakeGh()
        loop = _loop(_ctx(), gh)
        for ids in ([], ["nope"], ["a1", "done"], "a1", [1]):
            assert (await loop.file_cluster(ids, "T", "B"))["status"] == "invalid_ids"
            assert (await loop.dismiss(ids))["status"] == "invalid_ids"
        assert (await loop.file_cluster(["a1"], "  ", "B"))["status"] == "invalid_params"
        assert gh.calls == []
        assert (await _row("a1")).status == "pending"
    _run(go, tmp_path)


# -- dismiss / attach ---------------------------------------------------------

def test_dismiss_bulk_and_as_fixed(tmp_path):
    async def go():
        await _seed_theme()
        loop = _loop(_ctx(), FakeGh())
        r = await loop.dismiss(["a1", "a2"], fixed_by=2869)
        assert r["status"] == "dismissed" and r["count"] == 2
        for rid in ("a1", "a2"):
            row = await _row(rid)
            assert row.status == "dismissed"
            assert row.resolution == {"kind": "dismissed_fixed", "pr": 2869}
        r = await loop.dismiss(["b1"])
        assert (await _row("b1")).status == "dismissed" and (await _row("b1")).resolution is None
        assert (await loop.dismiss(["b1"]))["status"] == "invalid_ids"   # already out
        assert (await loop.dismiss(["a1"], fixed_by=-3))["status"] in ("invalid_ids",
                                                                        "invalid_params")
    _run(go, tmp_path)


def test_attach_issue_posts_one_comment_then_merges(tmp_path):
    async def go():
        await _seed_theme()
        gh = FakeGh()
        loop = _loop(_ctx(), gh, mode="off")
        r = await loop.attach_issue(["a1", "a2"], 2909)
        assert r["status"] == "attached" and r["issue"] == 2909
        assert len(gh.comments) == 1
        assert any(c[1].endswith("/issues/2909/comments") for c in gh.calls)
        assert "pytest died mid-run" in gh.comments[0] and "lost logs" in gh.comments[0]
        for rid in ("a1", "a2"):
            row = await _row(rid)
            assert (row.status, row.issue_number) == ("merged", 2909)
            assert row.resolution == {"kind": "attached_issue", "issue": 2909}
    _run(go, tmp_path)


def test_attach_issue_comment_failure_changes_no_row(tmp_path):
    async def go():
        await _seed_theme()
        gh = FakeGh()
        gh.fail_comment = True
        loop = _loop(_ctx(), gh)
        r = await loop.attach_issue(["a1"], 2909)
        assert r["status"] == "failed"
        assert (await _row("a1")).status == "pending"
    _run(go, tmp_path)


def test_evidence_then_mark_attached_session(tmp_path):
    async def go():
        await _seed_theme()
        loop = _loop(_ctx(), FakeGh())
        ev = await loop.evidence(["a1", "a2"])
        assert ev["status"] == "ok" and ev["count"] == 2
        assert "pytest died mid-run" in ev["text"] and "untrusted" in ev["text"]
        r = await loop.mark_attached_session(["a1", "a2"], "sess-9")
        assert r["status"] == "attached"
        row = await _row("a1")
        assert row.status == "dismissed"
        assert row.resolution == {"kind": "attached_session", "sessionId": "sess-9"}
        assert (await loop.mark_attached_session(["b1"], ""))["status"] == "invalid_params"
    _run(go, tmp_path)


# -- the provider seam --------------------------------------------------------

def test_provider_inbox_dispatches_every_op(tmp_path):
    async def go():
        await _seed_theme()

        class _Orch:
            pass
        orch = _Orch()
        orch._session_feedback_loop = _loop(_ctx(), FakeGh(), merge_fn=FakeMerges())
        p = SessionFeedbackActionProvider(orch)
        assert p.valid_inbox_ops == {"clusters", "draft", "file", "dismiss", "attach_issue",
                                     "evidence", "mark_attached_session"}
        assert p.inbox_write_ops <= p.valid_inbox_ops and "clusters" not in p.inbox_write_ops
        assert "clusters" in await p.inbox("clusters", {})
        assert (await p.inbox("draft", {"ids": ["a1"]}))["status"] == "ok"
        assert (await p.inbox("evidence", {"ids": ["a1"]}))["status"] == "ok"
        assert (await p.inbox("file", {"ids": ["a1"], "title": "T", "body": "B"}))["status"] == "filed"
        assert (await p.inbox("attach_issue", {"ids": ["a2"], "issue": 5}))["status"] == "attached"
        assert (await p.inbox("dismiss", {"ids": ["b1"], "fixedBy": 7}))["status"] == "dismissed"
        assert (await _row("b1")).resolution == {"kind": "dismissed_fixed", "pr": 7}
    _run(go, tmp_path)


# -- the model call door --------------------------------------------------------

def test_propose_merges_runs_one_sonnet_call_and_parses_the_reply():
    pre = [[Report(id="a", title="Restart killed run")], [Report(id="b", title="Restart again")]]
    ids = [cluster_id(["a"]), cluster_id(["b"])]
    calls: list[dict] = []

    def query(*, prompt, options):
        calls.append({"prompt": prompt, "options": options})

        async def gen():
            reply = '{"groups": [{"ids": ["%s", "%s"], "title": "Restarts"}]}' % tuple(ids)
            yield SimpleNamespace(content=[SimpleNamespace(text=reply)])
        return gen()

    parsed = asyncio.run(propose_merges(pre, _query_fn=query))
    assert parsed.groups == [MergeGroup(ids=tuple(ids), title="Restarts")]
    assert calls[0]["options"].model == "sonnet"
    assert calls[0]["options"].effort in ("low", "medium")
    assert ids[0] in calls[0]["prompt"]


def test_propose_merges_raises_when_the_call_fails():
    def query(*, prompt, options):
        async def gen():
            raise RuntimeError("boom")
            yield  # pragma: no cover
        return gen()

    with pytest.raises(RuntimeError):
        asyncio.run(propose_merges([[Report(id="a", title="x")], [Report(id="b", title="y")]],
                                   _query_fn=query))
