"""Pure stale-dependency classifier (server/pm/stale_deps.py, gap log 2026-10-11)."""
from __future__ import annotations

from server.pm.drift import DepEdgeView
from server.pm.stale_deps import classify_stale

R = "o/r"


def _e(src: int, dst: int | str, source: str, *, active: bool = True,
       state: str | None = "open") -> DepEdgeView:
    dst_ref = dst if isinstance(dst, str) else f"{R}#{dst}"
    return DepEdgeView(src_ref=f"{R}#{src}", dst_ref=dst_ref, source=source, active=active,
                       dst_state=state)


def _kinds(edges, *, open_srcs=None, bodies=None):
    out = classify_stale(edges, open_srcs=open_srcs or {e.src_ref for e in edges},
                         bodies=bodies or {})
    return {(s.src, s.blocker): (s.kind, s.prunable) for s in out}


def test_closed_native_blocker_is_stale_and_prunable():
    assert _kinds([_e(1, 2, "native", state="closed")]) == {
        (f"{R}#1", f"{R}#2"): ("native_resolved", True)}


def test_merged_pr_blocker_named_in_prose_is_stale_but_rederived():
    body = {f"{R}#1": "Depends on #9"}
    assert _kinds([_e(1, 9, "prose", state="merged")], bodies=body) == {
        (f"{R}#1", f"{R}#9"): ("derived_resolved", False)}


def test_vanished_prose_reference_is_stale_and_prunable_even_if_blocker_open():
    body = {f"{R}#1": "No dependency text any more"}
    assert _kinds([_e(1, 2, "prose", state="open")], bodies=body) == {
        (f"{R}#1", f"{R}#2"): ("prose_vanished", True)}


def test_still_open_blockers_are_not_listed():
    body = {f"{R}#1": "Depends on #2"}
    edges = [_e(1, 2, "native"), _e(1, 2, "prose"), _e(1, 3, "order")]
    assert _kinds(edges, bodies=body) == {}


def test_unknown_blocker_state_fails_closed():
    assert _kinds([_e(1, "x/y#4", "native", state=None)]) == {}


def test_inactive_rows_and_closed_sources_are_ignored():
    edges = [_e(1, 2, "native", active=False, state="closed"),
             _e(5, 2, "native", state="closed")]
    assert _kinds(edges, open_srcs={f"{R}#1"}) == {}


def test_unmirrored_body_never_reads_as_vanished():
    # body None = we do not know the text; never claim it vanished.
    assert _kinds([_e(1, 2, "prose", state="open")], bodies={f"{R}#1": None}) == {}


def test_native_row_wins_over_a_vanished_prose_row():
    # The native edge still blocks (open) — the pair is not stale.
    edges = [_e(1, 2, "native"), _e(1, 2, "prose")]
    assert _kinds(edges, bodies={f"{R}#1": "nothing"}) == {}


def test_order_edge_keeps_a_vanished_prose_pair_derived():
    edges = [_e(1, 2, "prose", state="closed"), _e(1, 2, "order", state="closed")]
    assert _kinds(edges, bodies={f"{R}#1": "nothing"}) == {
        (f"{R}#1", f"{R}#2"): ("derived_resolved", False)}
