"""#3072: no ViberPit surface tells a session to arm auto-merge on a PR labelled
``hold:human``, and fix/conflict dispatches leave a PR's merge state as they
found it (cairnea/KinDash#19061 was re-armed twice and entered the merge queue
unreviewed)."""
from __future__ import annotations

from typing import Any

import pytest

from server.automations import archive_audit as aa
from server.automations.completion_bar import PrFact, PrFacts, met_bar
from server.github.pr_status import (
    HUMAN_HOLD_LABEL,
    PullRequestInfo,
    _to_info,
    build_live_pr_query,
    build_pr_query,
    has_human_hold,
)
from server.sessions.pr_fix import (
    build_conflict_fix_prompt,
    build_consolidation_prompt,
    build_pr_fix_prompt,
)

ARM_STEP = "Arm auto-merge: `gh pr merge <#> --squash --auto`."


def _info(**kw) -> PullRequestInfo:
    base: dict[str, Any] = dict(number=7, url="https://github.com/o/r/pull/7", state="open",
                checks="failing", title="Legal docs", head_ref="feat/legal")
    base.update(kw)
    return PullRequestInfo(**base)


def _node(**kw) -> dict:
    node = {"number": 7, "url": "u", "state": "OPEN", "title": "t", "isDraft": False,
            "headRefOid": "", "updatedAt": "", "commits": {"nodes": []}}
    node.update(kw)
    return node


# --- the label reaches PullRequestInfo ---------------------------------------

def test_every_pr_query_selects_labels():
    assert "labels(first: 20)" in build_pr_query([("o/r", "feat/x")])
    assert "labels(first: 20)" in build_live_pr_query("o/r", 7)


def test_to_info_reads_labels_and_round_trips():
    info = _to_info(_node(labels={"nodes": [{"name": "hold:human"}, {"name": "docs"}]}), {})
    assert info.labels == ("hold:human", "docs")
    assert info.human_hold
    back = PullRequestInfo.from_dict(info.to_dict())
    assert back.labels == info.labels and back.human_hold


def test_to_info_without_labels_is_not_held():
    info = _to_info(_node(), {})
    assert info.labels == () and not info.human_hold


def test_has_human_hold_is_case_insensitive_and_exact():
    assert has_human_hold(["Hold:Human"])
    assert not has_human_hold(["hold:humans", "hold", "human"])
    assert not has_human_hold(None)


# --- the completion bar never nudges a held PR toward arming -----------------

def _fact(**kw) -> PrFact:
    base: dict[str, Any] = dict(number=7, state="open", is_draft=False, auto_merge_enabled=False,
                url="http://x/7", checks="passing")
    base.update(kw)
    return PrFact(**base)


def test_held_unarmed_pr_is_met_even_when_the_dispatch_says_arm():
    v = met_bar("fix the failing CI", ARM_STEP,
                PrFacts(has_repo=True, prs=(_fact(human_hold=True),)))
    assert v.met and v.confidence == "high"
    assert v.remedy is None


def test_held_draft_is_met_and_never_told_to_ready():
    v = met_bar("fix ci", ARM_STEP, PrFacts(
        has_repo=True, prs=(_fact(state="draft", is_draft=True, human_hold=True),)))
    assert v.met and v.remedy is None


def test_held_red_pr_is_told_to_fix_ci_not_to_arm():
    v = met_bar("fix ci", ARM_STEP,
                PrFacts(has_repo=True, prs=(_fact(checks="failing", human_hold=True),)))
    assert not v.met
    assert "--auto" not in v.remedy
    assert "do not arm" in v.remedy


def test_unheld_unarmed_pr_still_gets_the_arm_nudge():
    v = met_bar("fix ci", ARM_STEP, PrFacts(has_repo=True, prs=(_fact(),)))
    assert not v.met and "--squash --auto" in v.remedy


def test_from_info_carries_the_hold():
    assert PrFact.from_info(_info(labels=(HUMAN_HOLD_LABEL,))).human_hold
    assert not PrFact.from_info(_info()).human_hold


def test_facts_prompt_exposes_the_hold():
    facts = PrFacts(has_repo=True, prs=(_fact(human_hold=True),))
    assert facts.to_prompt()["prs"][0]["human_hold"] is True


def test_archive_audit_reads_the_hold():
    d = {"number": 7, "state": "OPEN", "isDraft": False, "autoMergeRequest": None,
         "mergeQueueEntry": None, "url": "http://x/7",
         "labels": {"nodes": [{"name": "hold:human"}]}}
    assert aa._pr_fact_from_gh(d).human_hold
    import inspect
    assert inspect.getsource(aa).count("labels(first:20)") >= 2


# --- fix / conflict prompts leave the merge state as found -------------------

def _builders():
    return (build_pr_fix_prompt, build_conflict_fix_prompt)


def test_held_pr_prompts_forbid_any_merge_command():
    for build in _builders():
        text = build("o/r", _info(labels=(HUMAN_HOLD_LABEL,)))
        assert HUMAN_HOLD_LABEL in text
        assert "Do NOT arm auto-merge" in text
        assert "--squash --auto" not in text


def test_unarmed_pr_prompts_forbid_arming():
    for build in _builders():
        text = build("o/r", _info())
        assert "do not arm auto-merge" in text
        assert "--squash --auto" not in text


def test_armed_pr_prompts_say_it_stays_armed():
    for build in _builders():
        text = build("o/r", _info(auto_merge_enabled=True))
        assert "already armed" in text
        assert "do not run `gh pr merge`" in text


def test_consolidation_prompt_refuses_a_held_pr():
    """Superseding a held PR would close it, and the prompt must never tell a
    session to apply a hold label — the orchestrator refuses the set first."""
    held = _info(number=1, labels=(HUMAN_HOLD_LABEL,))
    with pytest.raises(ValueError):
        build_consolidation_prompt("o/r", "master", [held, _info(number=2)])


def test_consolidation_without_holds_still_arms():
    text = build_consolidation_prompt("o/r", "master", [_info(number=1), _info(number=2)])
    assert "gh pr merge --squash --auto" in text
