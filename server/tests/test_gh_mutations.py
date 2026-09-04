import pytest
from server.pm.gh_mutations import (
    MAX_ALIASES, RateLimit, UnknownOption, build_batch, build_create_field,
    build_rate_limit_query, build_update_field_options, chunk, parse_batch,
    parse_rate_limit)
from server.pm.reconcile_diff import Mutation

CACHE = {"Status": {"field_id": "F_s", "options": {"Ready": "O_ready", "Done": "O_done"}},
         "Score": {"field_id": "F_sc", "options": {}}}


def test_build_batch_emits_aliased_mutations():
    muts = [Mutation("set_field", "PVTI_1", "Status", "Backlog", "Ready", "status:o/r#1"),
            Mutation("set_field", "PVTI_1", "Score", None, 480.0, "score:o/r#1"),
            Mutation("add_item", "PVT_1", None, None, "I_9", "membership:o/r#9"),
            Mutation("add_blocked_by", "I_5", None, None, "I_3", "compile:prose:o/r#5->o/r#3")]
    doc, alias_map = build_batch(muts, project_node_id="PVT_1", field_cache=CACHE)
    assert set(alias_map) == {"m0", "m1", "m2", "m3"}
    assert 'm0: updateProjectV2ItemFieldValue(input: {projectId: "PVT_1", itemId: "PVTI_1", fieldId: "F_s", value: {singleSelectOptionId: "O_ready"}})' in doc
    assert 'm1: updateProjectV2ItemFieldValue(input: {projectId: "PVT_1", itemId: "PVTI_1", fieldId: "F_sc", value: {number: 480.0}})' in doc
    assert 'm2: addProjectV2ItemById(input: {projectId: "PVT_1", contentId: "I_9"})' in doc
    assert 'm3: addBlockedBy(input: {issueId: "I_5", blockingIssueId: "I_3"})' in doc
    assert doc.startswith("mutation {")
    assert "rateLimit" not in doc


def test_build_batch_rejects_unknown_option_and_oversize():
    bad = [Mutation("set_field", "PVTI_1", "Status", None, "Nope", "x")]
    with pytest.raises(UnknownOption):
        build_batch(bad, project_node_id="PVT_1", field_cache=CACHE)
    too_many = [Mutation("add_item", "PVT_1", None, None, f"I_{i}", "m") for i in range(MAX_ALIASES + 1)]
    with pytest.raises(ValueError):
        build_batch(too_many, project_node_id="PVT_1", field_cache=CACHE)
    assert [len(c) for c in chunk(too_many)] == [MAX_ALIASES, 1]


def test_parse_batch_maps_errors_per_alias():
    payload = {"data": {"m0": {"projectV2Item": {"id": "PVTI_1"}}, "m1": None},
               "errors": [{"path": ["m1"], "message": "Could not resolve to a node"}]}
    alias_map = {"m0": Mutation("set_field", "PVTI_1", "Status", None, "Ready", "s"),
                 "m1": Mutation("add_item", "PVT_1", None, None, "I_x", "m")}
    results = parse_batch(payload, alias_map)
    assert results.errors == {"m0": None, "m1": "Could not resolve to a node"}
    assert results.failed_transiently() is False   # a pathed error IS evidence


def test_parse_batch_flags_a_document_level_error_as_transient():
    """A 502/abuse-limit error carries no ``path``: it fails all 20 aliases at once and
    says nothing about any of them, so the loop must record it without counting it
    toward the park-on-second-failure rule."""
    alias_map = {f"m{i}": Mutation("add_item", "PVT_1", None, None, f"I_{i}", "m")
                 for i in range(3)}
    results = parse_batch({"data": None, "errors": [{"message": "502 Bad Gateway"}]}, alias_map)
    assert results.failed_transiently() is True
    assert results.document_error == "502 Bad Gateway"
    assert set(results.errors.values()) == {"502 Bad Gateway"}
    # No errors at all and no data is still a per-mutation failure, not a transient one.
    quiet = parse_batch({"data": {"m0": None, "m1": None, "m2": None}}, alias_map)
    assert quiet.failed_transiently() is False and quiet.errors["m0"] == "no data returned"


def test_field_builders():
    assert 'dataType: SINGLE_SELECT' in build_create_field("PVT_1", "Stage", "SINGLE_SELECT", ["intake", "done"])
    assert '{name: "intake", color: GRAY, description: ""}' in build_create_field("PVT_1", "Stage", "SINGLE_SELECT", ["intake"])
    assert 'dataType: NUMBER, name: "Score"' in build_create_field("PVT_1", "Score", "NUMBER", None)
    assert 'updateProjectV2Field(input: {fieldId: "F_s", singleSelectOptions: [' in build_update_field_options("F_s", ["Backlog", "Done"])
    assert "rateLimit" not in build_create_field("PVT_1", "Score", "NUMBER", None)
    assert "rateLimit" not in build_update_field_options("F_s", ["Backlog", "Done"])


def test_rate_limit_query_and_parser():
    assert build_rate_limit_query() == "query { rateLimit { cost remaining resetAt } }"
    payload = {"data": {"rateLimit": {"cost": 2, "remaining": 4990, "resetAt": "x"}}}
    assert parse_rate_limit(payload) == RateLimit(2, 4990, "x")
    assert parse_rate_limit({"data": {}}) is None
