"""Pure attention-tier and rollup rules for the roadmap tree (spec §11)."""
from server.pm.tree import (
    TIERS, CapabilityIn, InitiativeIn, ProductIn, build_product_tree,
    capability_own_tier, incident_tier, initiative_tier, issue_tier, max_tier,
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
    assert capability_own_tier("live") == "live"
    assert capability_own_tier("proposed") == "proposed"
    assert capability_own_tier("delivering") == "idle"
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
