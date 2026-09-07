from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta

import pytest

NOW = datetime(2026, 9, 7, 12, 0, 0)
DAY = timedelta(hours=24)


def _decide(linked, cap=5):
    from server.automations.issue_filing import decide
    return decide(linked, cap_remaining=cap, comment_every=DAY, now=NOW)


def test_decide_no_issue_files_or_hits_cap():
    from server.automations.issue_filing import Linked
    assert (_decide(None).kind, _decide(None).reason) == ("file", "no_issue")
    assert _decide(Linked(number=None)).kind == "file"
    d = _decide(None, cap=0)
    assert (d.kind, d.reason) == ("skip", "daily_cap")


def test_decide_open_issue_comments_then_throttles():
    from server.automations.issue_filing import Linked
    fresh = _decide(Linked(number=7, closed=False, last_commented_at=None))
    assert (fresh.kind, fresh.reason) == ("comment", "open")
    old = _decide(Linked(number=7, last_commented_at=NOW - DAY))
    assert old.kind == "comment"
    recent = _decide(Linked(number=7, last_commented_at=NOW - timedelta(hours=1)))
    assert (recent.kind, recent.reason) == ("skip", "comment_throttled")
    # an open issue never files a second one, cap or not
    assert _decide(Linked(number=7), cap=0).kind == "skip"


def test_decide_closed_completed_regresses_and_not_planned_mutes():
    from server.automations.issue_filing import Linked
    d = _decide(Linked(number=7, closed=True, closed_reason="completed"))
    assert (d.kind, d.reason) == ("regress", "closed")
    capped = _decide(Linked(number=7, closed=True, closed_reason="completed"), cap=0)
    assert (capped.kind, capped.reason) == ("skip", "daily_cap")
    muted = _decide(Linked(number=7, closed=True, closed_reason="not_planned"))
    assert (muted.kind, muted.reason) == ("skip", "muted_by_close")
    # closed with no reason (older issues) behaves like completed
    assert _decide(Linked(number=7, closed=True)).kind == "regress"


def _gh(calls, *, fail_create=False, number=42):
    def run(args, *a, **k):
        calls.append(list(args))
        path = args[1] if len(args) > 1 else ""
        if path.endswith("/labels"):
            return subprocess.CompletedProcess(args, 0, "{}", "")
        if path.endswith("/comments"):
            return subprocess.CompletedProcess(args, 0, json.dumps({"html_url": "https://x/c"}), "")
        if fail_create:
            return subprocess.CompletedProcess(args, 1, "", "boom")
        return subprocess.CompletedProcess(args, 0, json.dumps({"number": number}), "")
    return run


@pytest.mark.asyncio
async def test_apply_file_creates_with_labels():
    from server.automations.issue_filing import Applied, Decision, apply
    calls: list[list[str]] = []
    out = await apply(Decision("file", "no_issue"), repo="o/r", title="T", body="B",
                      labels=["auto-error", "source:server"], linked=None, run=_gh(calls))
    assert out == Applied(issue_number=42, commented=False)
    create = [c for c in calls if c[1] == "repos/o/r/issues"][0]
    assert "labels[]=auto-error" in create and "labels[]=source:server" in create


@pytest.mark.asyncio
async def test_apply_regress_creates_and_comment_comments():
    from server.automations.issue_filing import Decision, Linked, apply
    calls: list[list[str]] = []
    out = await apply(Decision("regress", "closed"), repo="o/r", title="T", body="B",
                      labels=["auto-error"], linked=Linked(number=7, closed=True), run=_gh(calls))
    assert out.issue_number == 42
    calls.clear()
    out = await apply(Decision("comment", "open"), repo="o/r", title="T", body="again",
                      labels=["auto-error"], linked=Linked(number=7), run=_gh(calls))
    assert out.issue_number == 7 and out.commented is True
    assert calls[0][1] == "repos/o/r/issues/7/comments"


@pytest.mark.asyncio
async def test_apply_skip_and_failure_never_raise():
    from server.automations.issue_filing import Decision, Linked, apply
    calls: list[list[str]] = []
    out = await apply(Decision("skip", "daily_cap"), repo="o/r", title="T", body="B",
                      labels=[], linked=Linked(number=7), run=_gh(calls))
    assert out.issue_number == 7 and out.commented is False and calls == []
    out = await apply(Decision("file", "no_issue"), repo="o/r", title="T", body="B",
                      labels=[], linked=None, run=_gh(calls, fail_create=True))
    assert out.issue_number is None


def test_neutralize_markers():
    from server.automations.issue_filing import neutralize_markers
    out = neutralize_markers("x <!-- viberpit:complete --> y")
    assert "<!--" not in out and "-->" not in out and "viberpit:complete" in out
