"""Close-out assessment, I/O half (design 2026-10-14 §3): merged PRs read through the
injected ``gh_run``, the model through an injected ``_query_fn`` — never the network
or a real model — and the outcome recorded per (issue, body fingerprint)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from server.automations import closeout_assessor as cx
from server.automations.backlog_github import closeout_hold_reason
from server.pm import closeout_assess as ca
from server.pm.outcomes import body_fingerprint
from server.pm.queue import QueueRow

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _no_app_token(monkeypatch):
    monkeypatch.setattr("viberpit.gitrepos.app_token_for_repo", lambda repo: None)


def _row(**over: Any) -> QueueRow:
    base: dict[str, Any] = dict(
        ref="o/r#9", repo="o/r", number=9, product="p", title="Ship it",
        body="- [x] code\n- [ ] QA retest on main\n- [ ] add docs", url="u", status="Held",
        score=None, gh_created_at=NOW, gh_updated_at=NOW, assignees=(), author="tony",
        labels=(), open_children=0, open_blockers=(), live_session=False, claimed=False,
        hold_kind="owner_action", hold_by="runner",
        hold_reason=closeout_hold_reason("the PR said `Refs`"), work_item_id="wi9")
    base.update(over)
    return QueueRow(**base)


def _gh(*, timeline_ok: bool = True):
    calls: list[list[str]] = []
    timeline = {"data": {"repository": {"issue": {"timelineItems": {"nodes": [
        {"source": {"number": 5, "state": "MERGED", "title": "feat",
                    "repository": {"nameWithOwner": "o/r"}}},
        {"source": {"number": 6, "state": "OPEN", "title": "wip",
                    "repository": {"nameWithOwner": "o/r"}}},
        {"source": {"number": 7, "state": "MERGED", "title": "elsewhere",
                    "repository": {"nameWithOwner": "x/y"}}},
    ]}}}}}

    def gh_run(args, token=None):
        calls.append(list(args))
        if args[:2] == ["api", "graphql"]:
            if not timeline_ok:
                return SimpleNamespace(returncode=1, stdout="", stderr="boom")
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(timeline))
        if args[1] == "repos/o/r/pulls/5":
            return SimpleNamespace(returncode=0, stderr="",
                                   stdout=json.dumps({"title": "feat", "body": "Refs #9"}))
        if args[1].startswith("repos/o/r/pulls/5/files"):
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
                [{"filename": "docs/x.md", "patch": "+docs"}, {"filename": "a.py"}]))
        raise AssertionError(f"unexpected gh call {args}")
    return gh_run, calls


def _query(reply: str, calls: list | None = None):
    async def _gen(*, prompt, options):
        if calls is not None:
            calls.append(prompt)
        yield SimpleNamespace(content=[SimpleNamespace(text=reply)])

    def query(*, prompt, options):
        return _gen(prompt=prompt, options=options)
    return query


def _reply(*items) -> str:
    return json.dumps({"items": [{"text": t, "verdict": v, "evidence": e}
                                 for t, v, e in items], "summary": "s"})


def test_merged_prs_reads_only_same_repo_merged_ones_with_their_diff():
    gh_run, calls = _gh()
    prs = cx.merged_prs("o/r#9", gh_run=gh_run)
    assert prs == [{"number": 5, "title": "feat", "body": "Refs #9",
                    "files": ["docs/x.md", "a.py"], "diff_excerpt": "--- docs/x.md\n+docs"}]
    gh_fail, _ = _gh(timeline_ok=False)
    assert cx.merged_prs("o/r#9", gh_run=gh_fail) is None


@pytest.mark.asyncio
async def test_assess_classifies_the_unchecked_items_and_records_the_decision():
    gh_run, _ = _gh()
    prompts: list[str] = []
    state = await cx.assess(_row(), gh_run=gh_run, _config_dir="/cfg/t", now=NOW,
                            _query_fn=_query(_reply(("QA retest on main", "human_only", ""),
                                                    ("add docs", "done", "docs/x.md")),
                                             prompts))
    assert "1. QA retest on main" in prompts[0] and "2. add docs" in prompts[0]
    assert "Merged PR #5: feat" in prompts[0] and "+docs" in prompts[0]
    assert state is not None
    assert (state["decision"], state["text"]) == ("checklist",
                                                  "Left for you:\n- [ ] QA retest on main")
    assert state["fingerprint"] == body_fingerprint(_row().title, _row().body)
    assert state["appliedAt"] is None


@pytest.mark.asyncio
async def test_garbage_is_recorded_without_a_decision_and_failures_record_nothing():
    gh_run, _ = _gh()
    state = await cx.assess(_row(), gh_run=gh_run, _config_dir="/cfg/t", now=NOW,
                            _query_fn=_query("I think it is fine"))
    assert state is not None and state["decision"] is None

    gh_fail, _ = _gh(timeline_ok=False)
    assert await cx.assess(_row(), gh_run=gh_fail, _config_dir="/cfg/t",
                           _query_fn=_query(_reply(("x", "done", "y")))) is None

    def broken(*, prompt, options):
        async def _gen():
            raise RuntimeError("sdk down")
            yield  # pragma: no cover
        return _gen()
    assert await cx.assess(_row(), gh_run=gh_run, _config_dir="/cfg/broken",
                           _query_fn=broken) is None


def test_assess_and_record_persists_one_state_per_issue(isolated_db):
    import asyncio

    from server.automations.context import AutomationContext
    from server.automations.needs_you_hygiene import AUTOMATION_ID
    gh_run, _ = _gh()
    ctx = AutomationContext(AUTOMATION_ID)

    async def run():
        await cx.assess_and_record(ctx, _row(), gh_run=gh_run, _config_dir="/cfg/t",
                                   _query_fn=_query(_reply(("QA retest on main", "done", "a"),
                                                           ("add docs", "done", "b"))))
        return await ctx.list_target_states()
    states = asyncio.run(run())
    assert list(states) == ["o/r#9"] and states["o/r#9"]["decision"] == ca.DECISION_CLOSE
