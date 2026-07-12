# server/tests/test_consolidation_engine.py
"""Pure consolidation engine: scope derivation, clustering, selection guard."""
from __future__ import annotations

from server.automations.consolidation_engine import (
    ConsolidationCandidate, consolidation_target, derive_scope,
    parse_consolidation_target, suggest_clusters, validate_selection,
)


def _c(number, *, title="fix(deploy): x", checks="passing", mergeable="MERGEABLE"):
    return ConsolidationCandidate(
        number=number, title=title, url=f"https://x/pull/{number}",
        checks=checks, mergeable=mergeable, updated_at="2026-07-04T00:00:00Z",
        scope=derive_scope(title))


def test_derive_scope_reads_conventional_commit_scope():
    assert derive_scope("fix(deploy): mark phase done") == "deploy"
    assert derive_scope("feat(web)!: breaking") == "web"
    assert derive_scope("chore: no scope") == ""
    assert derive_scope("") == ""


def test_suggest_clusters_groups_same_scope_all_green_low_risk():
    cands = [_c(1), _c(2), _c(3, title="feat(web): y")]
    clusters = suggest_clusters(cands)
    assert len(clusters) == 1
    cl = clusters[0]
    assert cl.numbers == (1, 2)
    assert cl.risk == "low"
    assert "deploy" in cl.rationale and "all green" in cl.rationale


def test_suggest_clusters_flags_risky_when_a_member_is_red_or_conflicting():
    cands = [_c(1), _c(2, checks="failing")]
    cl = suggest_clusters(cands)[0]
    assert cl.risk == "risky"
    assert "all green" not in cl.rationale


def test_suggest_clusters_excludes_singletons_and_sorts_largest_first():
    cands = [_c(1), _c(2), _c(3), _c(10, title="feat(web): a"), _c(11, title="feat(web): b")]
    clusters = suggest_clusters(cands)
    assert [len(c.numbers) for c in clusters] == [3, 2]  # deploy(3) before web(2)


def test_validate_selection_requires_at_least_two_distinct():
    assert validate_selection([_c(1)]) == "too_few"
    assert validate_selection([_c(1), _c(1)]) == "too_few"
    assert validate_selection([_c(1), _c(2)]) is None


def test_consolidation_target_round_trip():
    target = consolidation_target("o/r", [2, 1, 2])
    assert target == "consolidate:o/r#1+2"
    assert parse_consolidation_target(target) == ("o/r", [1, 2])


def test_parse_consolidation_target_rejects_foreign_or_malformed_refs():
    assert parse_consolidation_target("pr:o/r#5") is None
    assert parse_consolidation_target("consolidate:o/r") is None
    assert parse_consolidation_target("consolidate:o/r#") is None
    assert parse_consolidation_target("consolidate:o/r#1+x") is None
