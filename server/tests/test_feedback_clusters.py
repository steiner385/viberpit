"""feedback_clusters — the pure Session Feedback clustering (#2936).

Pinned against the real 50 reports the operator worked down by hand on 2026-10-11
(``fixtures/session_feedback_50.json``) and a real Sonnet reply to ``merge_prompt``
over them (``fixtures/session_feedback_50_llm_reply.json``)."""
from __future__ import annotations

import json
import pathlib
from datetime import datetime

import pytest

from server.automations.feedback_clusters import (
    MergeGroup, Report, apply_merges, cluster_id, merge_prompt, parse_merge_reply,
    pre_cluster, signature, similarity, tokens,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _fixture() -> list[Report]:
    rows = json.loads((FIXTURES / "session_feedback_50.json").read_text())
    return [Report(id=r["id"], title=r["title"], category=r["category"], impact=r["impact"],
                   fingerprint=r["fingerprint"],
                   reported_at=datetime.fromisoformat(r["reported_at"])) for r in rows]


def _where(clusters, report_id):
    return next(i for i, c in enumerate(clusters) if report_id in {r.id for r in c})


# -- tokens / similarity ------------------------------------------------------

def test_tokens_drop_refs_counts_stopwords_and_stem():
    t = tokens("Backlog Runner re-dispatched red-watchdog #17388 ~35 times x7 after the verdict")
    assert {"backlog", "runner", "dispatch", "red", "watchdog", "verdict"} <= t
    assert not any(ch.isdigit() for w in t for ch in w)
    assert "the" not in t and "after" not in t and "re" not in t
    assert tokens("re-dispatches") == tokens("re-dispatched")
    assert tokens(None) == frozenset()


def test_similarity_is_the_overlap_coefficient():
    a, b = frozenset({"x", "y"}), frozenset({"x", "y", "z", "w"})
    assert similarity(a, b) == 1.0
    assert similarity(frozenset({"x", "q"}), b) == 0.5
    assert similarity(frozenset(), b) == 0.0


# -- the deterministic pass on the real fixture ---------------------------------

def test_pre_cluster_on_the_fixture_is_high_precision():
    reports = _fixture()
    pre = pre_cluster(reports)
    # Measured: average linkage at 0.4 → 26 pre-clusters (single linkage chained 23
    # reports into one blob). A sane band, not an exact pin, so a stopword tweak
    # does not fail the suite — the pairs below are the real assertion.
    assert 20 <= len(pre) <= 30
    assert sorted(r.id for c in pre for r in c) == sorted(r.id for r in reports)
    together = [
        ("sf-00", "sf-06"),  # red-watchdog #17388 re-dispatch, different counts
        ("sf-05", "sf-07"),  # watchdog re-dispatch, different watchdogs
        ("sf-04", "sf-14"),  # same fingerprint
        ("sf-09", "sf-11"),  # completion gate demands a PR from a read-only session
        ("sf-46", "sf-48"),  # PR linkage misses a differing head branch
        ("sf-41", "sf-42"),  # daemon restart kills verification
    ]
    for a, b in together:
        assert _where(pre, a) == _where(pre, b), (a, b)
    apart = [
        ("sf-00", "sf-04"),  # re-dispatch loop vs completion gate
        ("sf-36", "sf-28"),  # screenshots vs codex reload
        ("sf-41", "sf-46"),  # restart vs PR linkage
        ("sf-24", "sf-26"),  # hooksPath vs SPA deep link
        ("sf-23", "sf-32"),  # mark_complete tool vs review agent
    ]
    for a, b in apart:
        assert _where(pre, a) != _where(pre, b), (a, b)


def test_same_fingerprint_is_always_together_even_with_different_titles():
    reps = [Report(id="a", title="totally unrelated words", fingerprint="fp"),
            Report(id="b", title="something else entirely", fingerprint="fp"),
            Report(id="c", title="another thing", fingerprint="other")]
    pre = pre_cluster(reps)
    assert sorted(sorted(r.id for r in c) for c in pre) == [["a", "b"], ["c"]]


def test_pre_cluster_is_deterministic_under_input_order():
    reports = _fixture()
    a = [[r.id for r in c] for c in pre_cluster(reports)]
    b = [[r.id for r in c] for c in pre_cluster(list(reversed(reports)))]
    assert a == b
    assert signature(pre_cluster(reports)) == signature(pre_cluster(list(reversed(reports))))


# -- the model merge pass -------------------------------------------------------

def test_merge_prompt_lists_every_precluster_id_and_flags_untrusted_titles():
    pre = pre_cluster(_fixture())
    prompt = merge_prompt(pre)
    for c in pre:
        assert cluster_id(r.id for r in c) in prompt
    assert "untrusted" in prompt and '"groups"' in prompt


def test_recorded_sonnet_reply_collapses_the_fixture_into_themes():
    """The real reply (captured once; see the fixture's _note). It merges the 26
    pre-clusters into 18 clusters: five multi-report themes holding 37 of the 50
    reports, and 13 one-off reports the model — rightly — left alone."""
    reports = _fixture()
    pre = pre_cluster(reports)
    raw = json.loads((FIXTURES / "session_feedback_50_llm_reply.json").read_text())["reply"]
    parsed = parse_merge_reply(raw, [cluster_id(r.id for r in c) for c in pre])
    assert not parsed.degraded and parsed.groups
    clusters = apply_merges(pre, parsed.groups)
    assert sum(c.count for c in clusters) == 50
    assert len(clusters) < len(pre)
    themes = [c for c in clusters if c.count > 1]
    assert len(themes) <= 8
    assert sum(c.count for c in themes) >= 35
    by_report = {r.id: c.id for c in clusters for r in c.members}
    # The re-dispatch / triage-implement loop is one theme (sf-00 watchdog, sf-03 loop).
    assert by_report["sf-00"] == by_report["sf-03"] == by_report["sf-22"]
    # Completion nudge for an existing PR + PR head-branch linkage: one theme.
    assert by_report["sf-27"] == by_report["sf-47"] == by_report["sf-49"]
    # Restart kills in-flight work.
    assert by_report["sf-37"] == by_report["sf-41"] == by_report["sf-42"]
    # Unrelated one-offs stay apart.
    assert by_report["sf-36"] != by_report["sf-28"]
    biggest = clusters[0]
    assert biggest.themed and biggest.count >= 16
    assert biggest.title and len(biggest.title) <= 140


def test_parse_merge_reply_validates_the_model():
    valid = ["aaa", "bbb", "ccc", "ddd"]
    raw = ('Sure!\n```json\n{"groups": [{"ids": ["aaa", "bbb", "zzz"], "title": "  One\\n theme "},'
           ' {"ids": ["bbb", "ccc"], "title": "dup"}, {"ids": ["zzz"]}, "junk",'
           ' {"ids": "aaa"}]}\n```')
    parsed = parse_merge_reply(raw, valid)
    assert not parsed.degraded
    # unknown ids dropped; bbb's first appearance wins; emptied groups vanish
    assert parsed.groups == [MergeGroup(ids=("aaa", "bbb"), title="One theme"),
                             MergeGroup(ids=("ccc",), title="dup")]


@pytest.mark.parametrize("raw", ["", "no json here", '{"nope": 1}', '{"groups": "x"}', "[1, 2]"])
def test_unparseable_reply_merges_nothing_and_is_flagged(raw):
    parsed = parse_merge_reply(raw, ["aaa"])
    assert parsed.degraded and parsed.groups == []


def test_apply_merges_ignores_stale_ids_and_keeps_unmentioned_alone():
    t0 = datetime(2026, 10, 1)
    reps = [Report(id=f"r{i}", title=f"title {i}", category="c", impact=imp,
                   reported_at=t0.replace(hour=i)) for i, imp in
            enumerate(["minor", "blocked", "degraded"])]
    pre = [[r] for r in reps]
    ids = [cluster_id([r.id]) for r in reps]
    out = apply_merges(pre, [MergeGroup(ids=(ids[0], "gone", ids[1]), title="Theme")],
                       impact_rank={"minor": 0, "degraded": 1, "blocked": 2})
    assert [c.count for c in out] == [2, 1]
    merged = out[0]
    assert merged.title == "Theme" and merged.themed
    assert merged.impact == "blocked"
    assert [r.id for r in merged.members] == ["r1", "r0"]       # newest first
    assert merged.id == cluster_id(["r0", "r1"])
    d = merged.to_dict()
    assert d["count"] == 2 and d["firstReported"].startswith("2026-10-01T00")
    assert set(d) >= {"id", "title", "count", "category", "impact", "firstReported",
                      "lastReported", "members", "fingerprints", "themed"}
    assert out[1].title == "title 2" and not out[1].themed
