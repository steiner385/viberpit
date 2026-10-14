"""Parked review, I/O half (design 2026-10-14 §4): merged PRs read through the
injected ``gh_run``, the model through an injected ``_query_fn`` — never the network
or a real model — and one verdict recorded per parked issue."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from server.automations import parked_reviewer as rv
from server.pm import parked_review as pr
from server.pm.outcomes import body_fingerprint
from server.pm.queue import QueueRow

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _no_app_token(monkeypatch):
    monkeypatch.setattr("viberpit.gitrepos.app_token_for_repo", lambda repo: None)


def _row(**over: Any) -> QueueRow:
    base: dict[str, Any] = dict(
        ref="o/r#9", repo="o/r", number=9, product="p", title="Old idea", body="do x",
        url="u", status="Backlog", score=None, gh_created_at=NOW, gh_updated_at=NOW,
        assignees=(), author="tony", labels=("parked",), open_children=0, open_blockers=(),
        live_session=False, claimed=False, work_item_id="wi9")
    base.update(over)
    return QueueRow(**base)


def _gh(*, timeline_ok: bool = True):
    timeline = {"data": {"repository": {"issue": {"timelineItems": {"nodes": [
        {"source": {"number": 5, "state": "MERGED", "title": "feat: x",
                    "repository": {"nameWithOwner": "o/r"}}}]}}}}}

    def gh_run(args, token=None):
        if args[:2] == ["api", "graphql"]:
            if not timeline_ok:
                return SimpleNamespace(returncode=1, stdout="", stderr="boom")
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(timeline))
        if args[1] == "repos/o/r/pulls/5":
            return SimpleNamespace(returncode=0, stderr="",
                                   stdout=json.dumps({"title": "feat: x", "body": "Refs #9"}))
        if args[1].startswith("repos/o/r/pulls/5/files"):
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(
                [{"filename": "x.py", "patch": "+x"}]))
        raise AssertionError(f"unexpected gh call {args}")
    return gh_run


def _query(reply: str, prompts: list | None = None):
    async def _gen(*, prompt, options):
        if prompts is not None:
            prompts.append(prompt)
        yield SimpleNamespace(content=[SimpleNamespace(text=reply)])

    def query(*, prompt, options):
        return _gen(prompt=prompt, options=options)
    return query


@pytest.mark.asyncio
async def test_review_reads_the_merged_prs_and_records_a_cited_verdict():
    prompts: list[str] = []
    state = await rv.review(_row(), gh_run=_gh(), _config_dir="/cfg/t", now=NOW,
                            _query_fn=_query(json.dumps(
                                {"verdict": "close_fixed", "pr": 5, "evidence": "#5 adds x",
                                 "summary": "done by #5"}), prompts))
    assert "Parked issue: Old idea" in prompts[0] and "Merged PR #5: feat: x" in prompts[0]
    assert state is not None
    assert (state["verdict"], state["pr"], state["evidence"]) == ("close_fixed", 5, "#5 adds x")
    assert state["prs"] == [{"number": 5, "title": "feat: x",
                             "url": "https://github.com/o/r/pull/5"}]
    assert state["fingerprint"] == body_fingerprint("Old idea", "do x")


@pytest.mark.asyncio
async def test_an_unusable_reply_records_no_verdict_and_a_failed_read_records_nothing():
    state = await rv.review(_row(), gh_run=_gh(), _config_dir="/cfg/t",
                            _query_fn=_query('{"verdict": "close_fixed", "pr": 77, '
                                             '"evidence": "made up"}'))
    assert state is not None and state["verdict"] is None
    assert await rv.review(_row(), gh_run=_gh(timeline_ok=False), _config_dir="/cfg/t",
                           _query_fn=_query('{"verdict": "keep"}')) is None


def test_review_and_record_persists_under_the_parked_target(isolated_db):
    from server.automations.context import AutomationContext
    from server.automations.needs_you_hygiene import AUTOMATION_ID
    ctx = AutomationContext(AUTOMATION_ID)

    async def run():
        await rv.review_and_record(ctx, _row(), gh_run=_gh(), _config_dir="/cfg/t",
                                   _query_fn=_query('{"verdict": "unpark", "summary": "go"}'))
        return await ctx.list_target_states()
    states = asyncio.run(run())
    assert list(states) == [pr.item_target("o/r#9")]
    assert states["parked:o/r#9"]["verdict"] == "unpark"
