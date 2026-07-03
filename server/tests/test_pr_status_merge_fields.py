"""mergeable / merge_state_status / auto_merge_enabled: query, parse, round-trip."""
from __future__ import annotations

from server.github.pr_status import PullRequestInfo, _to_info, build_pr_query


def test_query_requests_new_fields():
    q = build_pr_query([("owner/repo", "feat/x")])
    assert "mergeable" in q
    assert "autoMergeRequest" in q
    assert "mergeStateStatus" in q


def test_to_info_populates_merge_fields():
    node = {"number": 5, "url": "u", "state": "OPEN", "title": "t", "isDraft": False,
            "headRefOid": "abc", "updatedAt": "", "mergeable": "CONFLICTING",
            "mergeStateStatus": "DIRTY", "autoMergeRequest": {"enabledAt": "2026-06-30T00:00:00Z"},
            "commits": {"nodes": []}}
    info = _to_info(node, {})
    assert info.mergeable == "CONFLICTING"
    assert info.merge_state_status == "DIRTY"
    assert info.auto_merge_enabled is True


def test_to_info_defaults_when_absent():
    node = {"number": 6, "url": "u", "state": "OPEN", "title": "t", "isDraft": False,
            "headRefOid": "", "updatedAt": "", "commits": {"nodes": []}}
    info = _to_info(node, {})
    assert info.mergeable == "UNKNOWN"
    assert info.merge_state_status == ""
    assert info.auto_merge_enabled is False


def test_round_trip_preserves_merge_fields():
    info = PullRequestInfo(number=5, url="u", state="open", checks="failing", title="t",
                           mergeable="CONFLICTING", merge_state_status="DIRTY", auto_merge_enabled=True)
    back = PullRequestInfo.from_dict(info.to_dict())
    assert back.mergeable == "CONFLICTING"
    assert back.merge_state_status == "DIRTY"
    assert back.auto_merge_enabled is True
