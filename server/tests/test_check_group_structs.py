from server.github.pr_status import CheckRow, CheckGroup


def test_checkrow_roundtrip():
    r = CheckRow(name="lint", workflow="CI", status="COMPLETED",
                 conclusion="success", duration_seconds=42, required=True, overdue=False)
    assert CheckRow.from_dict(r.to_dict()) == r


def test_checkrow_rem_roundtrip():
    from server.github.pr_status import CheckRow
    r = CheckRow(name="x", workflow="CI", status="IN_PROGRESS", conclusion="pending",
                 duration_seconds=60, required=True, overdue=False,
                 rem_low_seconds=60, rem_nominal_seconds=240, rem_high_seconds=540)
    assert CheckRow.from_dict(r.to_dict()).rem_high_seconds == 540


def test_checkgroup_roundtrip_and_defaults():
    g = CheckGroup(kind="head", checks=(
        CheckRow("lint", "CI", "COMPLETED", "success", 42, True, False),
    ), total_seconds=42, cost_usd=0.008, eta_seconds=None)
    d = g.to_dict()
    assert d["kind"] == "head" and d["total_seconds"] == 42 and d["cost_usd"] == 0.008
    assert CheckGroup.from_dict(d) == g
