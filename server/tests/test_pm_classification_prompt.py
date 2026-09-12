"""The feature-doc prompts must teach the shape the indexer reads: `parent:`
(the only grouping field, `featuredoc.py`), `labels:` (mechanical routing),
and a one-sentence outcome before `## Problem` (what the roadmap shows as the
summary, `feature_index._summary`). Agents that were only told the body
sections invented `area:` instead (KinDash, 18 docs, 2026-09)."""
import pytest

from server.pm.classification import build_coverage_critic_prompt, build_map_synthesis_prompt


@pytest.mark.parametrize("scope,kwargs", [
    ("initial", {}),
    ("diff", {"suggestion": "billing", "item_numbers": (1, 2)}),
])
def test_synthesis_prompt_teaches_parent_labels_and_the_outcome_sentence(scope, kwargs):
    prompt = build_map_synthesis_prompt(slug="p", repo="o/r", default_branch="main",
                                        open_count=1, closed_count=0, scope=scope, **kwargs)
    assert "  parent: <slug of the parent capability" in prompt
    assert "  labels:" in prompt and "bot-filed" in prompt
    assert "ONE sentence" in prompt and "before `## Problem`" in prompt
    assert "Never invent other frontmatter keys" in prompt


def test_critic_prompt_points_at_the_same_frontmatter_rules():
    prompt = build_coverage_critic_prompt(slug="p", repo="o/r", default_branch="main",
                                          branch="b", open_count=1, closed_count=0)
    assert "parent:" in prompt and "labels:" in prompt and "outcome sentence" in prompt
