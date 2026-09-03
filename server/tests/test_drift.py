from server.pm.drift import DepEdgeView, compute_drift

R = "steiner385/cc-account-switcher"


def _edge(src, dst, source, active=True, dst_state="open"):
    return DepEdgeView(src_ref=src, dst_ref=dst, source=source,
                       active=active, dst_state=dst_state)


def test_prose_edge_without_native_twin_is_drift():
    edges = [
        _edge(f"{R}#950", f"{R}#949", "prose"),
        _edge(f"{R}#951", f"{R}#950", "prose"),
        _edge(f"{R}#951", f"{R}#950", "native"),   # twin exists -> not drift
        _edge(f"{R}#952", f"{R}#951", "order"),    # order counts like prose
        _edge(f"{R}#953", f"{R}#952", "prose", active=False),  # inactive ignored
    ]
    report = compute_drift(edges, open_refs=set(), project_refs=set(), bodies={})
    assert report.prose_only == [
        {"src": f"{R}#950", "dst": f"{R}#949"},
        {"src": f"{R}#952", "dst": f"{R}#951"},
    ]


def test_unresolved_missing_and_checklist_epics():
    edges = [_edge(f"{R}#950", "steiner385/KinDash#12", "native", dst_state=None)]
    report = compute_drift(
        edges,
        open_refs={f"{R}#950", f"{R}#960"},
        project_refs={f"{R}#950"},
        bodies={f"{R}#960": "- [ ] #961\n- [x] #962", f"{R}#950": None},
    )
    assert report.unresolved_targets == [
        {"src": f"{R}#950", "dst": "steiner385/KinDash#12", "source": "native"}]
    assert report.missing_from_project == [f"{R}#960"]
    assert report.checklist_epics == [f"{R}#960"]


def test_checklist_epics_skips_refs_with_sub_issues():
    """A ref with a checklist body but present in epic_refs (already has real
    sub-issues) is NOT flagged; absent from epic_refs, it IS flagged."""
    edges: list[DepEdgeView] = []
    bodies = {f"{R}#960": "- [ ] #961\n- [x] #962"}
    # In epic_refs → skipped.
    report = compute_drift(
        edges, open_refs={f"{R}#960"}, project_refs={f"{R}#960"},
        bodies=bodies, epic_refs={f"{R}#960"})
    assert report.checklist_epics == []
    # Absent from epic_refs → flagged (existing behavior).
    report2 = compute_drift(
        edges, open_refs={f"{R}#960"}, project_refs={f"{R}#960"}, bodies=bodies)
    assert report2.checklist_epics == [f"{R}#960"]
