"""Pure attention-tier and rollup rules for the roadmap tree (spec §11)."""
from server.pm.tree import (
    TIERS, CapabilityIn, InitiativeIn, ProductIn, build_product_tree,
    capability_own_tier, derived_stage, incident_tier, initiative_tier,
    issue_reason, issue_tier, max_tier, rollup_epic_progress,
)


def test_tier_order_and_max():
    assert TIERS == ("needs-you", "blocked", "in-flight", "proposed", "live", "idle")
    assert max_tier("idle", "live", "blocked", "proposed") == "blocked"
    assert max_tier() == "idle"
    assert max_tier("bogus", "live") == "live"


def test_initiative_tier_rules():
    assert initiative_tier("awaiting_human", "design", 0, 0) == "needs-you"
    assert initiative_tier("active", "build", 0, 2) == "needs-you"      # epic needs-human wins
    assert initiative_tier("blocked", "build", 0, 0) == "blocked"
    assert initiative_tier("active", "build", 1, 0) == "blocked"        # open blocker in the epic
    assert initiative_tier("active", "intake", 0, 0) == "in-flight"
    assert initiative_tier("active", "deploy", 0, 0) == "in-flight"
    assert initiative_tier("proposed", "intake", 0, 0) == "proposed"
    assert initiative_tier("done", "done", 0, 0) == "live"
    assert initiative_tier("active", "done", 0, 0) == "live"
    assert initiative_tier(None, None, 0, 0) == "idle"


def test_issue_and_capability_and_incident_tiers():
    assert issue_tier("closed", "classified", False, None) == "live"
    assert issue_tier("merged", "classified", False, None) == "live"
    assert issue_tier("open", "needs_human", False, None) == "needs-you"
    assert issue_tier("open", "classified", False, "waiting_input") == "needs-you"
    assert issue_tier("open", "classified", True, None) == "blocked"
    assert issue_tier("open", "classified", False, "in_progress") == "in-flight"
    assert issue_tier("open", "classified", False, None) == "idle"
    # Board Status tier rows (spec §11): GhProjectItem.status mirrors the board.
    assert issue_tier("open", "classified", False, None, board_status="Needs human") == "needs-you"
    assert issue_tier("open", "classified", False, None, board_status="Blocked") == "blocked"
    assert issue_tier("open", "classified", False, None, board_status="In progress") == "in-flight"
    assert issue_tier("open", "classified", False, None, board_status="Backlog") == "idle"
    assert issue_tier("open", "classified", False, None, board_status=None) == "idle"
    assert capability_own_tier("live") == "live"
    assert capability_own_tier("proposed") == "proposed"
    assert capability_own_tier("delivering") == "idle"


def test_issue_reason_needs_spec_from_bounces():
    """D8: ONE rule (`bounced_out`) decides `needs_spec`; `issue_tier` never
    re-compares the count itself, only checks whether a reason was handed in."""
    assert issue_reason(bounces=2, give_up_after_bounces=2) == "needs_spec"
    assert issue_reason(bounces=1, give_up_after_bounces=2) is None
    assert issue_reason(bounces=5, give_up_after_bounces=0) is None   # 0 = never give up
    assert issue_reason() is None                                    # defaults: no bounces


def test_issue_tier_honours_reason():
    assert issue_tier("open", "classified", False, None, reason="needs_spec") == "needs-you"
    # A reason never overrides a resolved issue: closed/merged wins first.
    assert issue_tier("closed", "classified", False, None, reason="needs_spec") == "live"
    assert issue_tier("merged", "classified", False, None, reason="needs_spec") == "live"
    # No reason: the existing chain is unaffected.
    assert issue_tier("open", "classified", False, None) == "idle"
    assert incident_tier("open") == "needs-you"
    assert incident_tier("scheduled") == "needs-you"
    assert incident_tier("diagnosing") == "in-flight"
    assert incident_tier("dismissed") == "idle"


def _cap(slug, parent=None, state="live", issues=(), links=()):
    return CapabilityIn(id=f"id-{slug}", slug=slug, title=slug.title(), state=state, summary="",
                        doc_path=f"d/{slug}.md", commit_sha=None, pr_number=None, parent_slug=parent,
                        issue_tiers=tuple(issues), links=tuple(links))


def test_build_product_tree_nests_and_rolls_up():
    product = ProductIn(
        slug="example-product", name="Example",
        capabilities=(
            _cap("sessions"),
            _cap("dictation", parent="sessions", issues=("idle", "blocked")),
            _cap("stt", parent="dictation", state="delivering"),
            _cap("accounts"),
        ),
        initiatives=(
            InitiativeIn(slug="whisper", title="Whisper", stage="build", status="active",
                         primary_slug="stt", capability_slugs=("stt", "dictation"),
                         epic={"ref": "o/r#1", "url": "u", "total": 7, "done": 3, "blocked": 0, "needsHuman": 1}),
            InitiativeIn(slug="orphan", title="Orphan", stage="intake", status="proposed",
                         primary_slug=None, capability_slugs=(), epic=None),
        ),
        awaiting_triage=0, needs_home=0, incident_statuses=(),
    )
    tree = build_product_tree(product)
    roots = {c["slug"]: c for c in tree["capabilities"]}
    assert set(roots) == {"sessions", "accounts"}
    dictation = roots["sessions"]["children"][0]
    stt = dictation["children"][0]
    assert stt["initiatives"] == ["whisper"]
    assert stt["tier"] == "needs-you"                      # epic needsHuman=1 → initiative needs-you → rolls up
    assert dictation["tier"] == "needs-you"
    assert dictation["counts"] == {"openIssues": 2, "inFlight": 0, "blocked": 1, "needsYou": 1}
    assert roots["sessions"]["tier"] == "needs-you"
    assert roots["accounts"]["tier"] == "live"
    assert roots["accounts"]["counts"]["openIssues"] == 0
    by_slug = {i["slug"]: i for i in tree["initiatives"]}
    assert by_slug["whisper"]["primaryCapability"] == "stt"
    assert by_slug["whisper"]["capabilities"] == ["stt", "dictation"]
    assert by_slug["orphan"]["tier"] == "proposed"
    assert tree["tier"] == "needs-you"
    assert tree["counts"]["capabilities"] == 4
    assert tree["counts"]["needsYou"] == 1
    assert tree["counts"] == {"capabilities": 4, "inFlight": 0, "blocked": 1,
                              "needsYou": 1, "awaitingTriage": 0, "needsHome": 0}


def test_pseudo_nodes_and_incidents_lift_the_product_tier():
    quiet = ProductIn(slug="p", name="P", capabilities=(_cap("a"),), initiatives=(),
                      awaiting_triage=0, needs_home=0, incident_statuses=())
    assert build_product_tree(quiet)["tier"] == "live"
    triage = ProductIn(slug="p", name="P", capabilities=(_cap("a"),), initiatives=(),
                       awaiting_triage=3, needs_home=0, incident_statuses=())
    assert build_product_tree(triage)["tier"] == "needs-you"
    assert build_product_tree(triage)["counts"]["awaitingTriage"] == 3
    diag = ProductIn(slug="p", name="P", capabilities=(_cap("a"),), initiatives=(),
                     awaiting_triage=0, needs_home=0, incident_statuses=("diagnosing",))
    assert build_product_tree(diag)["tier"] == "in-flight"


def test_edge_cases_root_fallback_and_empty_product():
    empty = ProductIn(slug="p", name="P", capabilities=(), initiatives=(),
                      awaiting_triage=0, needs_home=0, incident_statuses=())
    empty_tree = build_product_tree(empty)
    assert empty_tree["tier"] == "idle"
    assert empty_tree["counts"] == {"capabilities": 0, "inFlight": 0, "blocked": 0,
                                    "needsYou": 0, "awaitingTriage": 0, "needsHome": 0}
    assert empty_tree["capabilities"] == []

    product = ProductIn(
        slug="p2", name="P2",
        capabilities=(_cap("orphaned", parent="nope"),),
        initiatives=(
            InitiativeIn(slug="floating", title="Floating", stage="build", status="active",
                         primary_slug="nope", capability_slugs=(), epic=None),
        ),
        awaiting_triage=0, needs_home=0, incident_statuses=(),
    )
    tree = build_product_tree(product)
    roots = {c["slug"]: c for c in tree["capabilities"]}
    assert "orphaned" in roots     # parent_slug points nowhere real → treated as a root
    assert roots["orphaned"]["initiatives"] == []
    by_slug = {i["slug"]: i for i in tree["initiatives"]}
    assert by_slug["floating"]["tier"] == "in-flight"
    assert tree["counts"]["inFlight"] == 1


def test_advance_ready_initiative_is_needs_you_with_reason():
    from server.pm.tree import initiative_reason
    # build stage, every epic child closed → the human has to press Advance
    assert initiative_tier("active", "build", 0, 0, epic_total=3, epic_done=3) == "needs-you"
    assert initiative_reason("active", "build", 3, 3) == "advance"
    # not ready: still open children, or no epic at all
    assert initiative_tier("active", "build", 0, 0, epic_total=3, epic_done=2) == "in-flight"
    assert initiative_reason("active", "build", 3, 2) is None
    assert initiative_tier("active", "build", 0, 0, epic_total=0, epic_done=0) == "in-flight"
    assert initiative_reason("active", "build", 0, 0) is None
    # other stages never say advance, even with a finished epic
    assert initiative_reason("active", "deploy", 3, 3) is None
    assert initiative_reason("done", "done", 3, 3) is None


def test_map_node_carries_reason():
    ini = InitiativeIn(slug="s", title="S", stage="build", status="active",
                       primary_slug="cap", capability_slugs=("cap",),
                       epic={"ref": "o/r#1", "url": "u", "total": 2, "done": 2,
                             "blocked": 0, "needsHuman": 0})
    cap = CapabilityIn(id="f1", slug="cap", title="Cap", state="live", summary=None,
                       doc_path=None, commit_sha=None, pr_number=None, parent_slug=None,
                       issue_tiers=())
    tree = build_product_tree(ProductIn(slug="p", name="P", capabilities=(cap,),
                                        initiatives=(ini,), awaiting_triage=0, needs_home=0))
    node = tree["initiatives"][0]
    assert node["tier"] == "needs-you"
    assert node["reason"] == "advance"
    assert tree["capabilities"][0]["tier"] == "needs-you"
    assert tree["counts"]["needsYou"] == 1


def test_initiative_blocked_reason_names_the_cause():
    from server.pm.tree import initiative_blocked_reason
    assert initiative_blocked_reason("blocked", 0) == "blocked"
    # The row's own status wins over the epic count: the human clears the row first.
    assert initiative_blocked_reason("blocked", 3) == "blocked"
    assert initiative_blocked_reason("active", 2) == "epic_blocked"
    assert initiative_blocked_reason("active", 0) is None
    assert initiative_blocked_reason(None, 0) is None


def test_blocked_initiative_node_carries_its_reason():
    """The map row is the only place the reason is computed (constitution VI):
    a blocked row says why, and a needs-you row keeps the advance reason it had."""
    from server.pm.tree import InitiativeIn, _initiative_node
    epic = {"ref": "o/r#1", "url": "u", "total": 3, "done": 1, "blocked": 2, "needsHuman": 0}
    node = _initiative_node(InitiativeIn("x", "X", "build", "active", None, (), epic), "p")
    assert node["tier"] == "blocked"
    assert node["reason"] == "epic_blocked"
    own = _initiative_node(InitiativeIn("y", "Y", "plan", "blocked", None, (), None), "p")
    assert (own["tier"], own["reason"]) == ("blocked", "blocked")
    # Needs-you outranks blocked and keeps its own reason.
    ready = {"ref": "o/r#1", "url": "u", "total": 3, "done": 3, "blocked": 0, "needsHuman": 0}
    adv = _initiative_node(InitiativeIn("z", "Z", "build", "active", None, (), ready), "p")
    assert (adv["tier"], adv["reason"]) == ("needs-you", "advance")
    # An awaiting_human row with a blocked sub-issue is needs-you with NO reason:
    # the reason field says why it needs a human beyond its status, and it does not.
    park = _initiative_node(InitiativeIn("w", "W", "build", "awaiting_human", None, (), epic), "p")
    assert (park["tier"], park["reason"]) == ("needs-you", None)


def test_rollup_sums_leaf_descendants_and_leaves_leaves_alone():
    entries = {
        "prog": {"ref": "o/r#1", "url": "u1", "total": 2, "done": 0, "blocked": 0, "needsHuman": 0},
        "p1": {"ref": "o/r#2", "url": "u2", "total": 4, "done": 1, "blocked": 1, "needsHuman": 0},
        "p2": {"ref": "o/r#3", "url": "u3", "total": 6, "done": 3, "blocked": 0, "needsHuman": 2},
        "solo": {"ref": "o/r#9", "url": "u9", "total": 5, "done": 5, "blocked": 0, "needsHuman": 0},
        "noepic": None,
    }
    parent_of = {"prog": None, "p1": "prog", "p2": "prog", "solo": None, "noepic": None}
    out = rollup_epic_progress(entries, parent_of)
    assert out["prog"] == {"ref": "o/r#1", "url": "u1", "total": 10, "done": 4, "blocked": 1, "needsHuman": 2}
    assert out["p1"] == entries["p1"] and out["solo"] == entries["solo"] and out["noepic"] is None


def test_rollup_walks_grandchildren_and_a_program_without_an_epic_entry():
    entries = {"prog": None, "mid": None, "leaf": {"ref": "o/r#5", "url": "u", "total": 3, "done": 1, "blocked": 0, "needsHuman": 0}}
    parent_of = {"prog": None, "mid": "prog", "leaf": "mid"}
    out = rollup_epic_progress(entries, parent_of)
    assert out["prog"] == {"ref": None, "url": None, "total": 3, "done": 1, "blocked": 0, "needsHuman": 0}
    assert out["mid"] == out["prog"] | {"ref": None, "url": None}


def test_derived_stage_is_the_earliest_unfinished_phase():
    # NOTE: the plan brief's draft of this assertion expected "plan" here, but that
    # contradicts both the brief's own sample implementation (min by
    # INITIATIVE_STAGES.index) and mirror_store.back_sync_stage's comment ("a
    # program's stage is derived from its phases" = the laggard, i.e. the
    # earliest/least-advanced non-done stage). "design" (index 1) is earlier than
    # "plan" (index 2) in INITIATIVE_STAGES, so it is the correct expected value —
    # flagged in the Task 4 report rather than silently left as a typo.
    assert derived_stage(["design", "plan", "build"]) == "design"
    assert derived_stage(["done", "deploy"]) == "deploy"
    assert derived_stage(["done", "done"]) == "done"
    assert derived_stage([]) is None
    assert derived_stage([None, "build"]) == "build"
    # No entry recognized (e.g. every phase's mirrored stage was garbage or
    # unset) → None, so callers fall back to the row's own stage rather than
    # a phantom "done". A "done" entry mixed with an unrecognized one is still
    # "done": every RECOGNIZED entry is done, and at least one is recognized.
    assert derived_stage([None, None]) is None
    assert derived_stage(["bogus"]) is None
    assert derived_stage([None, "done"]) == "done"


def test_program_reason_tier_and_counts_come_from_its_phases():
    phase_epic = {"ref": "o/r#2", "url": "u", "total": 4, "done": 4, "blocked": 0, "needsHuman": 0}
    product = ProductIn(
        slug="p", name="P", capabilities=(_cap("a11y"),),
        initiatives=(
            InitiativeIn(slug="audit", title="UI audit", stage="design", status="active",
                         primary_slug="a11y", capability_slugs=("a11y",),
                         epic={"ref": "o/r#1", "url": "u", "total": 4, "done": 4, "blocked": 0, "needsHuman": 0}),
            InitiativeIn(slug="audit-01", title="01", stage="build", status="active",
                         primary_slug="a11y", capability_slugs=("a11y",), epic=phase_epic,
                         parent_slug="audit"),
            InitiativeIn(slug="audit-02", title="02", stage="design", status="active",
                         primary_slug="a11y", capability_slugs=("a11y",), epic=None,
                         parent_slug="audit"),
        ),
        awaiting_triage=0, needs_home=0, incident_statuses=())
    tree = build_product_tree(product)
    by_slug = {i["slug"]: i for i in tree["initiatives"]}
    assert by_slug["audit-01"]["reason"] == "advance" and by_slug["audit-01"]["tier"] == "needs-you"
    assert by_slug["audit"]["reason"] is None                 # a program never "advances"
    assert by_slug["audit"]["tier"] == "needs-you"            # …but inherits its phase's tier
    assert by_slug["audit"]["hasChildren"] is True and by_slug["audit"]["parentInitiative"] is None
    assert by_slug["audit-01"]["parentInitiative"] == "audit"
    a11y = tree["capabilities"][0]
    assert a11y["counts"]["needsYou"] == 1 and a11y["counts"]["inFlight"] == 1   # leaves only, no double count
    assert tree["counts"]["needsYou"] == 1


def test_rollup_breaks_a_cycle_into_roots():
    """A mutual parent_slug pair must never blow the stack: 'unknown or cyclic
    parent counts as a root' applies here too, so each entry passes through
    unchanged — both are simultaneously roots and leaves of themselves."""
    entries = {
        "a": {"ref": "o/r#1", "url": "u1", "total": 2, "done": 1, "blocked": 0, "needsHuman": 0},
        "b": {"ref": "o/r#2", "url": "u2", "total": 3, "done": 0, "blocked": 1, "needsHuman": 0},
    }
    parent_of = {"a": "b", "b": "a"}
    out = rollup_epic_progress(entries, parent_of)
    assert out == entries


def test_cyclic_initiative_parents_become_roots():
    """A mutual parent_slug pair must not silently become two "programs" that
    hide each other's counts (both hasChildren=True bumps nothing): the
    sanitize rule demotes both to independent, leaf roots."""
    product = ProductIn(
        slug="p", name="P", capabilities=(_cap("infra"),),
        initiatives=(
            InitiativeIn(slug="x", title="X", stage="build", status="blocked",
                         primary_slug="infra", capability_slugs=("infra",), epic=None,
                         parent_slug="y"),
            InitiativeIn(slug="y", title="Y", stage="build", status="blocked",
                         primary_slug="infra", capability_slugs=("infra",), epic=None,
                         parent_slug="x"),
        ),
        awaiting_triage=0, needs_home=0, incident_statuses=())
    tree = build_product_tree(product)
    by_slug = {i["slug"]: i for i in tree["initiatives"]}
    assert by_slug["x"]["parentInitiative"] is None and by_slug["x"]["hasChildren"] is False
    assert by_slug["y"]["parentInitiative"] is None and by_slug["y"]["hasChildren"] is False
    infra = tree["capabilities"][0]
    assert infra["counts"]["blocked"] == 2
