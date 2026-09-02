"""TIER_ORDER is the single ordered source of truth for model tiers."""
import re
from pathlib import Path

from server.automations.model_pick import AUTO_MODEL_POOL
from server.sessions.model_catalog import TIER_ORDER, model_tier, tier_rank


def test_tier_order_is_cheapest_first():
    assert TIER_ORDER == ["haiku", "sonnet", "opus", "fable"]


def test_tier_rank_orders_aliases():
    assert tier_rank("haiku") < tier_rank("sonnet") < tier_rank("opus") < tier_rank("fable")


def test_tier_rank_handles_pinned_ids_and_context_tags():
    assert tier_rank("claude-fable-5-1") == tier_rank("fable") == tier_rank("fable[1m]")
    assert tier_rank("opusplan") == tier_rank("opus")


def test_tier_rank_is_none_for_untiered_models():
    for m in (None, "", "default", "best", "gpt-4"):
        assert tier_rank(m) is None, m


def test_model_tier_still_agrees_with_tier_order():
    for tier in TIER_ORDER:
        assert model_tier(tier) == tier


def test_auto_model_pool_derives_from_tier_order():
    """model_pick must not redeclare the ordering (constitution VI)."""
    assert AUTO_MODEL_POOL == TIER_ORDER


def test_web_tiers_match_server_tier_order():
    """web/src/session/modelName.ts TIERS is the web mirror — keys must agree."""
    ts = (Path(__file__).resolve().parents[2] / "web/src/session/modelName.ts").read_text()
    m = re.search(r"export const TIERS[^=]*=\s*\{([^}]*)\}", ts)
    assert m, "exported TIERS object not found in modelName.ts"
    keys = set(re.findall(r"(\w+)\s*:", m.group(1)))
    assert keys == set(TIER_ORDER)


def test_panel_has_no_private_tier_label_copy():
    """PendingModelApprovalPanel must use the shared TIERS, not a local copy."""
    panel = (Path(__file__).resolve().parents[2]
             / "web/src/components/session/PendingModelApprovalPanel.tsx").read_text()
    assert "TIER_LABELS" not in panel
