"""Pure unit tests for should_free_slot — no I/O, no fixtures."""
from __future__ import annotations

from server.github.pr_status import PullRequestInfo
from server.automations.backlog_selection import should_free_slot


def _pr(**kw):
    base = dict(number=1, url="u", state="open", checks="passing", title="t")
    base.update(kw)
    return PullRequestInfo(**base)


def test_pr_doctor_active_frees_on_open_plus_automerge():
    assert should_free_slot(_pr(state="open", auto_merge_enabled=True), True) is True
    assert should_free_slot(_pr(state="open", auto_merge_enabled=False), True) is False


def test_no_pr_doctor_frees_only_on_merge():
    assert should_free_slot(_pr(state="open", auto_merge_enabled=True), False) is False
    assert should_free_slot(_pr(state="merged"), False) is True


def test_merged_always_frees():
    assert should_free_slot(_pr(state="merged"), True) is True
