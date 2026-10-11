"""Product automation policy (spec 2026-09-12 §4): one schema, one validated reading."""
from __future__ import annotations
import pytest

from server.automations.product_policy import (
    DISPATCH_MODES, PRODUCT_POLICY_SCHEMA, SESSION_ASSISTANCE_MODES, effective_policy, trusted_authors,
)


def test_schema_keys_and_defaults():
    keys = [f.key for f in PRODUCT_POLICY_SCHEMA]
    assert keys == ["dispatch", "queue_min_status", "max_in_flight", "capacity_mode", "max_pipeline_prs",
                    "ladder_top", "give_up_after_bounces", "default_model",
                    "respect_dependencies", "skip_labels", "skip_self_closing_trackers",
                    "trusted_authors", "auto_escalate_match",
                    "auto_escalate_budget", "triage_auto_accept", "triage_auto_accept_dispatch_off",
                    "triage_dismiss_bot_artifacts", "risk_tolerance",
                    "max_initiatives_in_flight", "unattended_model_floor", "session_assistance",
                    "artifact_layout"]
    assert DISPATCH_MODES == ("off", "propose", "auto")
    assert effective_policy(None) == {
        "dispatch": "off", "queue_min_status": "Ready", "max_in_flight": 1, "max_pipeline_prs": None,
        "capacity_mode": "auto",
        "ladder_top": "fable", "give_up_after_bounces": 0, "default_model": "auto",
        "respect_dependencies": True, "skip_labels": [], "skip_self_closing_trackers": True,
        "trusted_authors": [],
        "auto_escalate_match": [],
        "auto_escalate_budget": 0, "triage_auto_accept": "off", "triage_auto_accept_dispatch_off": False,
        "triage_dismiss_bot_artifacts": False, "risk_tolerance": "cautious",
        "max_initiatives_in_flight": 2, "unattended_model_floor": "none", "session_assistance": "on",
        "artifact_layout": "docs_sdlc"}


def test_skip_labels_is_additive_string_list_defaulting_empty():
    field = {f.key: f for f in PRODUCT_POLICY_SCHEMA}["skip_labels"]
    assert field.type == "string-list" and field.default == []
    assert effective_policy({"dispatch": "auto", "skip_labels": ["on-hold", "needs-design"]}
                            )["skip_labels"] == ["on-hold", "needs-design"]
    # A non-list value is a corrupt row, not a schema violation the client fixes:
    # falls back to pure defaults like any other unparseable field (never raises).
    assert effective_policy({"dispatch": "auto", "skip_labels": "on-hold"})["skip_labels"] == []


def test_skip_self_closing_trackers_defaults_on_and_reads_through_one_function():
    """#2683: the queue skips self-closing trackers unless a product opts back in;
    ``skips_self_closing_trackers`` is the one reading every queue caller passes."""
    from server.automations.product_policy import skips_self_closing_trackers
    field = {f.key: f for f in PRODUCT_POLICY_SCHEMA}["skip_self_closing_trackers"]
    assert field.type == "bool" and field.default is True
    assert skips_self_closing_trackers(effective_policy({"dispatch": "auto"})) is True
    assert skips_self_closing_trackers(None) is True and skips_self_closing_trackers({}) is True
    off = effective_policy({"dispatch": "auto", "skip_self_closing_trackers": False})
    assert skips_self_closing_trackers(off) is False


def test_ladder_top_choices_are_the_tier_order_and_max_attempts_is_gone():
    from server.automations.product_policy import PRODUCT_POLICY_SCHEMA, effective_policy
    from server.sessions.model_catalog import TIER_ORDER
    field = next(f for f in PRODUCT_POLICY_SCHEMA if f.key == "ladder_top")
    assert field.choices == list(TIER_ORDER) and field.default == "fable"
    assert "max_attempts" not in effective_policy({"dispatch": "auto"})
    # a stored policy still carrying the retired key validates (unknown keys are dropped, not rejected)
    assert effective_policy({"dispatch": "auto", "max_attempts": 2})["ladder_top"] == "fable"


def test_weight_is_retired():
    """Removal (spec 2026-09-20 D6, #2103): ``weight`` fed only the ``weighted``
    selection strategy, which now orders exactly like ``priority``. A stored row
    from before the removal still validates — the retired key is dropped, not
    rejected — same treatment as ``max_attempts``."""
    from server.automations.product_policy import PRODUCT_POLICY_SCHEMA, effective_policy
    assert "weight" not in {f.key for f in PRODUCT_POLICY_SCHEMA}
    assert "weight" not in effective_policy({"dispatch": "auto", "weight": 3})


def test_merge_policy_patch_drops_a_retired_key_a_stale_client_still_sends():
    """#1960 runner edges tidy: ``merge_policy_patch``'s retired-key path (a stale
    client PATCH still carrying ``max_attempts`` from before its removal) was
    exercised only transitively, through ``effective_policy``'s own coverage —
    this exercises the merge itself. Neither the stored row nor the stale patch
    should ever resurrect the key into what gets persisted."""
    from server.automations.product_policy import merge_policy_patch
    merged = merge_policy_patch({"dispatch": "auto"}, {"max_attempts": 5, "ladder_top": "opus"})
    assert merged is not None
    assert "max_attempts" not in merged
    assert merged["ladder_top"] == "opus"
    # A retired key already sitting in the STORED policy (a pre-retirement row)
    # is dropped the same way, even when the patch itself never mentions it.
    merged = merge_policy_patch({"dispatch": "auto", "max_attempts": 2}, {"ladder_top": "haiku"})
    assert "max_attempts" not in merged
    assert merged == {"dispatch": "auto", "ladder_top": "haiku"}


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


def test_give_up_after_bounces_is_the_ninth_field_defaulting_to_never():
    """Phase 3 / D8: the ONE bounce threshold. ``min=0`` because 0 is meaningful —
    "never give up" — while a negative threshold would make ``bounced_out`` true on
    the very first finalised outcome.

    The default is 0 since the model ladder landed (spec 2026-09-18 §4, decision 2:
    "park only above the top tier"): a default of 2 capped every product at two
    attempts, so ``ladder_top`` was unreachable and the owner hand-off never fired.
    It stays available as an optional STRICTER dial."""
    field = {f.key: f for f in PRODUCT_POLICY_SCHEMA}["give_up_after_bounces"]
    assert field.type == "int"
    assert field.default == 0
    assert field.min == 0
    assert effective_policy(None)["give_up_after_bounces"] == 0
    assert effective_policy({"give_up_after_bounces": 2})["give_up_after_bounces"] == 2
    assert effective_policy({"give_up_after_bounces": 5})["give_up_after_bounces"] == 5
    # A negative threshold degrades the WHOLE policy to defaults.
    assert effective_policy({"give_up_after_bounces": -1})["give_up_after_bounces"] == 0


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
    assert merge_policy_patch({"dispatch": "off"}, {"max_in_flight": 3}) == {"dispatch": "off", "max_in_flight": 3}
    assert merge_policy_patch(None, {"max_in_flight": 3}) == {"max_in_flight": 3}
    assert merge_policy_patch({"dispatch": "auto"}, None) is None
    assert merge_policy_patch({"unknown": True}, None) is None


@pytest.mark.parametrize("current,patch", [
    ({"dispatch": "turbo"}, {"max_in_flight": 3}),
    ({"unknown": True}, {"max_in_flight": 3}),
    ([], {"max_in_flight": 3}),
    ({}, {"unknown": True}),
    ({}, {"dispatch": "turbo"}),
])
def test_sparse_policy_merge_rejects_corruption(current, patch):
    from server.automations.product_policy import merge_policy_patch
    with pytest.raises(ValueError):
        merge_policy_patch(current, patch)


def test_skip_labels_normalises_the_policy_list_for_the_queue():
    """ONE reading of ``skip_labels`` for every consumer (the runner's pool,
    ``GET /api/pm/queue``, the escalation read model, hold hygiene): lowercased,
    stripped, a set — a missing or absent policy reads as nothing to skip."""
    from server.automations.product_policy import skip_labels
    assert skip_labels({"skip_labels": [" Forced-Deploy-Audit ", "red-watchdog"]}) == frozenset(
        {"forced-deploy-audit", "red-watchdog"})
    assert skip_labels({}) == frozenset()
    assert skip_labels(None) == frozenset()
    assert skip_labels(effective_policy(None)) == frozenset()


def test_session_assistance_defaults_on_and_only_takes_on_or_off():
    """#1611 phase 3: the default preserves today's global Autopilot behaviour; a
    product can only turn it off, never widen it."""
    assert SESSION_ASSISTANCE_MODES == ("on", "off")
    assert effective_policy({"session_assistance": "off"})["session_assistance"] == "off"
    with pytest.raises(ValueError):
        from server.automations.product_policy import merge_policy_patch
        merge_policy_patch(None, {"session_assistance": "auto"})


def test_trusted_authors_reader_normalises():
    # A bare app login is dropped on read (it could only match a squatting user).
    assert trusted_authors({"trusted_authors": [" Steiner385 ", "", "viberpit",
                                                "github-actions", "App/Viberpit"]}) == \
        frozenset({"steiner385", "app/viberpit"})
    assert trusted_authors(None) == frozenset()


@pytest.mark.parametrize("entry", ["github-actions", "viberpit", " Viberpit ", "GITHUB-ACTIONS"])
def test_merge_policy_patch_rejects_a_bare_app_login_in_trusted_authors(entry):
    """A bare ``github-actions``/``viberpit`` entry would trust a USER who registers that
    login (both are unregistered) -- apps are written ``app/<slug>``."""
    from server.automations.product_policy import merge_policy_patch
    with pytest.raises(ValueError, match=r"app/"):
        merge_policy_patch(None, {"trusted_authors": ["tony", entry]})


def test_merge_policy_patch_accepts_the_app_form_and_ordinary_users():
    from server.automations.product_policy import merge_policy_patch
    merged = merge_policy_patch(None, {"trusted_authors": ["app/viberpit", "app/github-actions",
                                                           "steiner385"]})
    assert merged == {"trusted_authors": ["app/viberpit", "app/github-actions", "steiner385"]}
