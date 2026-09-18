"""Product automation policy (spec 2026-09-12 §4): one schema, one validated reading."""
from __future__ import annotations
import pytest

from server.automations.product_policy import (
    DISPATCH_MODES, PRODUCT_POLICY_SCHEMA, effective_policy,
)


def test_schema_keys_and_defaults():
    keys = [f.key for f in PRODUCT_POLICY_SCHEMA]
    assert keys == ["dispatch", "queue_min_status", "max_in_flight", "weight",
                    "ladder_top", "give_up_after_bounces", "default_model",
                    "respect_dependencies", "triage_auto_accept",
                    "risk_tolerance", "max_initiatives_in_flight", "unattended_model_floor"]
    assert DISPATCH_MODES == ("off", "propose", "auto")
    assert effective_policy(None) == {
        "dispatch": "off", "queue_min_status": "Ready", "max_in_flight": 1, "weight": 1,
        "ladder_top": "fable", "give_up_after_bounces": 2, "default_model": "auto",
        "respect_dependencies": True, "triage_auto_accept": "off",
        "risk_tolerance": "cautious", "max_initiatives_in_flight": 2,
        "unattended_model_floor": "none"}


def test_ladder_top_choices_are_the_tier_order_and_max_attempts_is_gone():
    from server.automations.product_policy import PRODUCT_POLICY_SCHEMA, effective_policy
    from server.sessions.model_catalog import TIER_ORDER
    field = next(f for f in PRODUCT_POLICY_SCHEMA if f.key == "ladder_top")
    assert field.choices == list(TIER_ORDER) and field.default == "fable"
    assert "max_attempts" not in effective_policy({"dispatch": "auto"})
    # a stored policy still carrying the retired key validates (unknown keys are dropped, not rejected)
    assert effective_policy({"dispatch": "auto", "max_attempts": 2})["ladder_top"] == "fable"


def test_an_in_memory_annotation_survives_a_re_read_as_a_dropped_key():
    """The Backlog Runner hangs ``ladder_top_configured`` on its snapshot (the
    ``ladder_top`` the approval gate clamped away). Re-reading such a policy must be
    idempotent: an annotation is dropped, NEVER read as a corrupt row — that would
    degrade the product to dispatch ``off`` silently. It is still never persisted:
    ``merge_policy_patch`` validates directly and rejects it."""
    from server.automations.product_policy import (
        PRODUCT_POLICY_ANNOTATIONS, effective_policy, merge_policy_patch)
    annotated = {**effective_policy({"dispatch": "auto", "ladder_top": "opus"}),
                 "ladder_top_configured": "fable"}
    assert "ladder_top_configured" in PRODUCT_POLICY_ANNOTATIONS
    reread = effective_policy(annotated)
    assert reread["dispatch"] == "auto" and reread["ladder_top"] == "opus"
    assert "ladder_top_configured" not in reread
    with pytest.raises(ValueError):
        merge_policy_patch(None, {"ladder_top_configured": "fable"})


def test_effective_policy_fills_defaults_and_falls_back_on_garbage():
    assert effective_policy({"dispatch": "auto", "max_in_flight": 3})["max_in_flight"] == 3
    assert effective_policy({"dispatch": "auto"})["queue_min_status"] == "Ready"
    # An unknown key or a bad enum never takes a product OFF the safe default.
    assert effective_policy({"dispatch": "turbo"})["dispatch"] == "off"
    assert effective_policy({"nope": 1})["dispatch"] == "off"
    assert effective_policy("not a dict")["dispatch"] == "off"  # type: ignore[arg-type]


def test_queue_min_status_choices_are_the_queue_vocabulary():
    from server.pm.queue import MIN_STATUS_OPTIONS
    field = next(f for f in PRODUCT_POLICY_SCHEMA if f.key == "queue_min_status")
    assert field.choices == list(MIN_STATUS_OPTIONS)


def test_triage_auto_accept_is_the_eighth_field_with_the_spec_choices():
    field = {f.key: f for f in PRODUCT_POLICY_SCHEMA}["triage_auto_accept"]
    assert field.type == "enum"
    assert field.default == "off"
    assert field.choices == ["off", "high", "medium"]
    assert effective_policy(None)["triage_auto_accept"] == "off"
    assert effective_policy({"triage_auto_accept": "medium"})["triage_auto_accept"] == "medium"
    # A bad value degrades the WHOLE policy to defaults (effective_policy's contract).
    assert effective_policy({"triage_auto_accept": "always"})["triage_auto_accept"] == "off"


def test_give_up_after_bounces_is_the_ninth_field_defaulting_to_two():
    """Phase 3 / D8: the ONE bounce threshold. ``min=0`` because 0 is meaningful —
    "never give up" — while a negative threshold would make ``bounced_out`` true on
    the very first finalised outcome."""
    field = {f.key: f for f in PRODUCT_POLICY_SCHEMA}["give_up_after_bounces"]
    assert field.type == "int"
    assert field.default == 2
    assert field.min == 0
    assert effective_policy(None)["give_up_after_bounces"] == 2
    assert effective_policy({"give_up_after_bounces": 0})["give_up_after_bounces"] == 0
    assert effective_policy({"give_up_after_bounces": 5})["give_up_after_bounces"] == 5
    # A negative threshold degrades the WHOLE policy to defaults.
    assert effective_policy({"give_up_after_bounces": -1})["give_up_after_bounces"] == 2


def test_risk_tolerance_is_the_gate_vocabulary_defaulting_to_cautious():
    """Spec 2026-09-14 D2/D3: the dial's choices are imported from the gate
    registry, never retyped here (constitution VI)."""
    from server.sdlc.gates import RISK_TOLERANCES
    field = {f.key: f for f in PRODUCT_POLICY_SCHEMA}["risk_tolerance"]
    assert field.type == "enum"
    assert field.default == "cautious"
    assert field.choices == list(RISK_TOLERANCES)
    assert effective_policy({"risk_tolerance": "bold"})["risk_tolerance"] == "bold"
    # Garbage degrades the WHOLE policy to defaults: a bad row can only be more careful.
    assert effective_policy({"risk_tolerance": "reckless"})["risk_tolerance"] == "cautious"


def test_max_initiatives_in_flight_defaults_to_two_and_zero_pauses():
    field = {f.key: f for f in PRODUCT_POLICY_SCHEMA}["max_initiatives_in_flight"]
    assert field.type == "int"
    assert field.default == 2
    assert field.min == 0
    assert effective_policy({"max_initiatives_in_flight": 0})["max_initiatives_in_flight"] == 0
    assert effective_policy({"max_initiatives_in_flight": -1})["max_initiatives_in_flight"] == 2


def test_unattended_model_floor_is_none_or_a_model_tier():
    """Spec 2026-09-14 D11: the tier vocabulary comes from
    server/sessions/model_request.py:MODEL_TIERS, never retyped (constitution VI).
    Schema only in phase 1; phase 2 applies it to unattended spawns."""
    from server.sessions.model_request import MODEL_TIERS
    field = {f.key: f for f in PRODUCT_POLICY_SCHEMA}["unattended_model_floor"]
    assert field.type == "enum"
    assert field.default == "none"
    assert field.choices == ["none", *MODEL_TIERS]
    assert effective_policy({"unattended_model_floor": "opus"})["unattended_model_floor"] == "opus"
    assert effective_policy({"unattended_model_floor": "gpt-5"})["unattended_model_floor"] == "none"


def test_sparse_policy_merge_preserves_explicit_choices():
    from server.automations.product_policy import merge_policy_patch
    assert merge_policy_patch({"dispatch": "off"}, {"weight": 3}) == {"dispatch": "off", "weight": 3}
    assert merge_policy_patch(None, {"weight": 3}) == {"weight": 3}
    assert merge_policy_patch({"dispatch": "auto"}, None) is None
    assert merge_policy_patch({"unknown": True}, None) is None


@pytest.mark.parametrize("current,patch", [
    ({"dispatch": "turbo"}, {"weight": 3}),
    ({"unknown": True}, {"weight": 3}),
    ([], {"weight": 3}),
    ({}, {"unknown": True}),
    ({}, {"dispatch": "turbo"}),
])
def test_sparse_policy_merge_rejects_corruption(current, patch):
    from server.automations.product_policy import merge_policy_patch
    with pytest.raises(ValueError):
        merge_policy_patch(current, patch)
