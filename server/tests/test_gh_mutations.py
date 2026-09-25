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


def test_render_remove_blocked_by_matches_the_live_schema():
    """`removeBlockedBy(input: {issueId, blockingIssueId})` — introspected live
    2026-09-11 (RemoveBlockedByInput: issueId ID!, blockingIssueId ID!). Ignores
    the project arguments exactly as add_blocked_by does."""
    from server.pm.gh_mutations import build_batch
    from server.pm.reconcile_diff import remove_blocked_by_mutation
    m = remove_blocked_by_mutation("I_blocked", "I_blocker", initiated_by="dep-remove:o/r#5")
    doc, alias_map = build_batch([m], project_node_id="", field_cache={})
    assert doc == ('mutation { m0: removeBlockedBy(input: {issueId: "I_blocked", '
                   'blockingIssueId: "I_blocker"}) { clientMutationId } }')
    assert alias_map == {"m0": m}


def test_build_batch_rejects_unknown_option_and_oversize():
    bad = [Mutation("set_field", "PVTI_1", "Status", None, "Nope", "x")]
    with pytest.raises(UnknownOption):
        build_batch(bad, project_node_id="PVT_1", field_cache=CACHE)
    too_many = [Mutation("add_item", "PVT_1", None, None, f"I_{i}", "m") for i in range(MAX_ALIASES + 1)]
    with pytest.raises(ValueError):
        build_batch(too_many, project_node_id="PVT_1", field_cache=CACHE)
    assert [len(c) for c in chunk(too_many)] == [MAX_ALIASES, 1]


def test_held_resolves_by_name_like_any_other_status():
    from server.pm.desired_state import STATUS_HELD
    cache = {"Status": {"field_id": "F_s", "options": {"Held": "O_held"}}}
    m = Mutation("set_field", "PVTI_1", "Status", None, STATUS_HELD, "status:o/r#1")
    doc, _ = build_batch([m], project_node_id="PVT_1", field_cache=cache)
    assert 'value: {singleSelectOptionId: "O_held"}' in doc


def test_held_with_no_option_cached_is_an_unknown_option():
    from server.pm.desired_state import STATUS_HELD
    cache = {"Status": {"field_id": "F_s", "options": {"Ready": "O_ready"}}}
    m = Mutation("set_field", "PVTI_1", "Status", None, STATUS_HELD, "status:o/r#1")
    with pytest.raises(UnknownOption):
        build_batch([m], project_node_id="PVT_1", field_cache=cache)


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


#: Verbatim from production (gh_write_journal, 2026-09-20): GitHub's uniqueness
#: validation when the blocked-by edge the reconciler is adding already exists.
ALREADY_EXISTS = ("An error occurred while adding the blocking issue to the issue. "
                  "Validation failed: Target issue has already been taken")
#: Same prefix, a genuine conflict — must stay a failure.
CYCLE = ("An error occurred while adding the blocking issue to the issue. Validation "
         "failed: this dependency would create a cycle where the target is already "
         "blocked by the source")


def test_parse_batch_reads_an_existing_blocked_by_edge_as_converged():
    """``addBlockedBy`` rejected because the edge is already there is the desired
    state, not a defect: the reconciler must record it applied so the key lands in
    ``applied_keys`` and ``compile_edges`` stops re-planning it (#1947)."""
    alias_map = {"m0": Mutation("add_blocked_by", "I_5", None, None, "I_3", "compile:order:a->b"),
                 "m1": Mutation("add_blocked_by", "I_9", None, None, "I_8", "compile:order:c->d")}
    results = parse_batch(
        {"data": {"m0": None, "m1": None},
         "errors": [{"path": ["m0"], "message": ALREADY_EXISTS},
                    {"path": ["m1"], "message": CYCLE}]}, alias_map)
    assert results.converged == {"m0"}
    assert results.failed_transiently() is False
    # The cycle rejection is a real conflict a human has to resolve — still a failure.
    assert results.errors["m1"] == CYCLE


def test_converged_is_scoped_to_the_edge_mutation_that_earns_it():
    """The idempotency read is per mutation kind, not a global message denylist: the
    same words from any other mutation are an ordinary failure."""
    alias_map = {"m0": Mutation("add_item", "PVT_1", None, None, "I_9", "membership:o/r#9"),
                 "m1": Mutation("remove_blocked_by", "I_5", None, None, "I_3", "dep_remove")}
    results = parse_batch(
        {"data": {"m0": None, "m1": None},
         "errors": [{"path": ["m0"], "message": ALREADY_EXISTS},
                    {"path": ["m1"], "message": ALREADY_EXISTS}]}, alias_map)
    assert results.converged == frozenset()
    assert results.errors == {"m0": ALREADY_EXISTS, "m1": ALREADY_EXISTS}


def test_a_document_level_failure_is_never_read_as_converged():
    """A pathless error says nothing about any alias (#1508), so it must not be
    mistaken for "the edge was already there" even when its text would match."""
    alias_map = {"m0": Mutation("add_blocked_by", "I_5", None, None, "I_3", "compile:order:a->b")}
    results = parse_batch({"data": None, "errors": [{"message": ALREADY_EXISTS}]}, alias_map)
    assert results.failed_transiently() is True and results.converged == frozenset()


def test_field_builders():
    assert 'dataType: SINGLE_SELECT' in build_create_field("PVT_1", "Stage", "SINGLE_SELECT", ["intake", "done"])
    assert '{name: "intake", color: GRAY, description: ""}' in build_create_field("PVT_1", "Stage", "SINGLE_SELECT", ["intake"])
    assert 'dataType: NUMBER, name: "Score"' in build_create_field("PVT_1", "Score", "NUMBER", None)
    assert 'updateProjectV2Field(input: {fieldId: "F_s", singleSelectOptions: [' in build_update_field_options("F_s", ["Backlog", "Done"])
    assert "rateLimit" not in build_create_field("PVT_1", "Score", "NUMBER", None)
    assert "rateLimit" not in build_update_field_options("F_s", ["Backlog", "Done"])


def test_update_field_options_echoes_existing_option_ids():
    """Live scratch-board finding (#987): ``updateProjectV2Field`` regenerates the id of
    every option whose input entry omits ``id`` — and an item assigned to a regenerated
    option comes back with a null value. Echoing the id preserves both. Options the board
    does not have yet (the appended spec names) carry no id and are created fresh."""
    doc = build_update_field_options("F_s", ["Todo", "Backlog"], option_ids={"Todo": "f75ad846"})
    assert '{id: "f75ad846", name: "Todo", color: GRAY, description: ""}' in doc
    assert '{name: "Backlog", color: GRAY, description: ""}' in doc   # no id ⇒ created fresh
    # no map (or a name the board doesn't have) degrades to the create-fresh form
    assert '{id:' not in build_update_field_options("F_s", ["Todo"])


def test_update_field_options_preserves_colour_and_description_when_given():
    """``color``/``description`` are required members of the option input and the mutation
    REPLACES the set, so a caller holding the board's current values must be able to send
    them back — otherwise every preserved column is repainted gray and its description
    erased (the rename one-shot's hazard)."""
    doc = build_update_field_options(
        "F_s", ["Todo", "Held", "New"],
        option_ids={"Todo": "o1", "Held": "o2"},
        option_attrs={"Todo": {"color": "blue", "description": 'the "next" pile'},
                      "Held": {"color": "PURPLE", "description": ""},
                      "New": {"color": "NOT_A_COLOR", "description": None}})
    assert '{id: "o1", name: "Todo", color: BLUE, description: "the \\"next\\" pile"}' in doc
    assert '{id: "o2", name: "Held", color: PURPLE, description: ""}' in doc
    # An unknown colour (or a missing/non-string description) falls back to the default
    # rather than interpolating an arbitrary token into the enum position.
    assert '{name: "New", color: GRAY, description: ""}' in doc


def test_update_field_options_without_attrs_is_unchanged_for_the_bootstrap():
    """The reconciler's field bootstrap passes no attrs and must keep its old rendering:
    a brand-new option gets GitHub's defaults, which is what a new option should get."""
    assert (build_update_field_options("F_s", ["Todo", "Backlog"], option_ids={"Todo": "o1"})
            == build_update_field_options("F_s", ["Todo", "Backlog"], option_ids={"Todo": "o1"},
                                          option_attrs=None))
    assert 'color: GRAY, description: ""' in build_update_field_options("F_s", ["Todo"])


def test_rate_limit_query_and_parser():
    assert build_rate_limit_query() == "query { rateLimit { cost remaining resetAt } }"
    payload = {"data": {"rateLimit": {"cost": 2, "remaining": 4990, "resetAt": "x"}}}
    assert parse_rate_limit(payload) == RateLimit(2, 4990, "x")
    assert parse_rate_limit({"data": {}}) is None


def _proc(stdout="", returncode=0, stderr=""):
    import subprocess
    return subprocess.CompletedProcess(args=["gh"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def test_payload_from_gh_accepts_partial_errors_only_when_asked():
    """``gh api graphql`` exits 1 whenever the response has an ``errors`` array, even
    with valid ``data`` for the other aliases. A mutation batch WANTS that payload
    (per-alias verdicts); a mirror read does not (a null node would read as deleted)."""
    from server.pm.gh_mutations import payload_from_gh
    body = '{"data": {"m0": {"id": 1}, "m1": null}, "errors": [{"path": ["m1"], "message": "cycle"}]}'
    partial = _proc(body, 1, "gh: cycle")
    assert payload_from_gh(partial, partial_ok=True)["errors"][0]["path"] == ["m1"]
    with pytest.raises(RuntimeError, match="cycle"):
        payload_from_gh(partial, partial_ok=False)
    # A clean exit returns the body either way.
    assert payload_from_gh(_proc('{"data": {"ok": 1}}', 0), partial_ok=False) == {"data": {"ok": 1}}
    # Transport failure: non-zero exit with no GraphQL body raises in both modes.
    for proc in (_proc("", 1, "gh: HTTP 502"), _proc("<html>", 1, "gh: HTTP 502"),
                 _proc('{"message": "Bad credentials"}', 1, "gh: HTTP 401")):
        with pytest.raises(RuntimeError):
            payload_from_gh(proc, partial_ok=True)
