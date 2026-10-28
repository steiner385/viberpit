from server.pm.dep_plan import DepRowView, ObservedDep, observed_deps, plan_dep_rows
from server.pm.gh_projects import IssueRelView

REPO = "steiner385/viberpit"


def _rel(number, blocked_by=(), parent=None, subs=()):
    return IssueRelView(repo=REPO, number=number, blocked_by=list(blocked_by),
                        parent_ref=parent, sub_issue_refs=list(subs))


def test_observed_deps_merges_native_and_prose():
    rels = [
        _rel(945, subs=[f"{REPO}#950", f"{REPO}#951", f"{REPO}#952"]),
        _rel(950, blocked_by=["steiner385/KinDash#12"], parent=f"{REPO}#945"),
    ]
    bodies = {f"{REPO}#950": "Depends on #949", f"{REPO}#945": None}
    deps = set(observed_deps(REPO, rels, bodies))
    assert deps == {
        ObservedDep(f"{REPO}#950", "steiner385/KinDash#12", "native"),
        ObservedDep(f"{REPO}#950", f"{REPO}#949", "prose"),
    }


def test_sub_issue_list_order_is_not_a_dependency():
    """#3157: an epic's independent children were chained 19905←19904,
    19906←19905, 19907←19906 from sub-issue order alone, so three of four sat
    blocked. Spec 003 §4 — list order is never trusted; only native or prose
    edges gate."""
    epic = "cairnea/KinDash"
    rels = [IssueRelView(repo=epic, number=19908, blocked_by=[], parent_ref=None,
                         sub_issue_refs=[f"{epic}#{n}" for n in range(19904, 19908)])]
    bodies = {f"{epic}#19908": "Order: critical first, then the small one, "
                               "then the large shared-component fixes."}
    assert observed_deps(epic, rels, bodies) == []


def test_plan_dep_rows_diffs_create_reactivate_deactivate():
    observed = [ObservedDep("a#1", "a#2", "native"), ObservedDep("a#1", "a#3", "prose")]
    existing = [
        DepRowView("row1", "a#1", "a#2", "native", active=False),   # -> reactivate
        DepRowView("row2", "a#1", "a#9", "native", active=True),    # -> deactivate
        DepRowView("row3", "a#1", "a#9", "prose", active=False),    # inactive+gone -> untouched
    ]
    plan = plan_dep_rows(observed, existing)
    assert plan.create == [ObservedDep("a#1", "a#3", "prose")]
    assert plan.reactivate == ["row1"]
    assert plan.deactivate == ["row2"]
