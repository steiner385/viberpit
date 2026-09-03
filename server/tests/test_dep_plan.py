from server.pm.dep_plan import DepRowView, ObservedDep, observed_deps, plan_dep_rows
from server.pm.gh_projects import IssueRelView

REPO = "steiner385/cc-account-switcher"


def _rel(number, blocked_by=(), parent=None, subs=()):
    return IssueRelView(repo=REPO, number=number, blocked_by=list(blocked_by),
                        parent_ref=parent, sub_issue_refs=list(subs))


def test_observed_deps_merges_native_prose_and_order():
    rels = [
        _rel(945, subs=[f"{REPO}#950", f"{REPO}#951", f"{REPO}#952"]),
        _rel(950, blocked_by=["steiner385/KinDash#12"], parent=f"{REPO}#945"),
    ]
    bodies = {f"{REPO}#950": "Depends on #949", f"{REPO}#945": None}
    deps = set(observed_deps(REPO, rels, bodies))
    assert ObservedDep(f"{REPO}#950", "steiner385/KinDash#12", "native") in deps
    assert ObservedDep(f"{REPO}#950", f"{REPO}#949", "prose") in deps
    # order: consecutive sibling chain, first sub-issue has no order dep
    assert ObservedDep(f"{REPO}#951", f"{REPO}#950", "order") in deps
    assert ObservedDep(f"{REPO}#952", f"{REPO}#951", "order") in deps
    assert not any(d.dst_ref == d.src_ref for d in deps)


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
