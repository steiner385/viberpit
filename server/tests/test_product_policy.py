"""Product automation policy (spec 2026-09-12 §4): one schema, one validated reading."""
from __future__ import annotations

from server.automations.product_policy import (
    DISPATCH_MODES, PRODUCT_POLICY_SCHEMA, effective_policy,
)


def test_schema_keys_and_defaults():
    keys = [f.key for f in PRODUCT_POLICY_SCHEMA]
    assert keys == ["dispatch", "queue_min_status", "max_in_flight", "weight",
                    "max_attempts", "give_up_after_bounces", "default_model",
                    "respect_dependencies", "triage_auto_accept"]
    assert DISPATCH_MODES == ("off", "propose", "auto")
    assert effective_policy(None) == {
        "dispatch": "off", "queue_min_status": "Ready", "max_in_flight": 1, "weight": 1,
        "max_attempts": 2, "give_up_after_bounces": 2, "default_model": "auto",
        "respect_dependencies": True, "triage_auto_accept": "off"}


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
