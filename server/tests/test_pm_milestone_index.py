"""plan_ladder_sync — pure planner: definition file text vs DB milestone rows."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from server.pm.milestone_def import MILESTONES_PATH
from server.pm.milestone_index import LadderRowsView, MilestoneRowView, plan_ladder_sync

FIXTURE = (Path(__file__).parent / "fixtures" / "milestones" / "ladder.yaml").read_text()
REPO = "cairnea/KinDash"
NOW = datetime(2026, 11, 5, 12, 0, 0)
KEYS = ("household", "friendly", "community", "public")


def _rows(*, current: str | None = "household", removed: tuple[str, ...] = (),
          keys: tuple[str, ...] = KEYS) -> LadderRowsView:
    return LadderRowsView(exists=True, milestones=tuple(
        MilestoneRowView(key=k, state="current" if k == current else "upcoming",
                         removed=k in removed)
        for k in keys))


def _plan(rows: LadderRowsView, text: str | None, sha: str = "sha1"):
    return plan_ladder_sync(rows, text=text, sha=sha, primary_repo=REPO, now=NOW)


def _blocks(text: str) -> tuple[str, list[str], str]:
    """Split the fixture into (head, milestone blocks, tail); a block starts at a
    line that is exactly-indented ``  - key: `` (gate items are indented deeper)."""
    head, _, rest = text.partition("milestones:\n")
    body, _, tail = rest.partition("numbers:\n")
    blocks = ["  - key: " + b for b in ("\n" + body).split("\n  - key: ") if b]
    return head + "milestones:\n", [b if b.endswith("\n") else b + "\n" for b in blocks], "numbers:\n" + tail


def _without(text: str, key: str) -> str:
    head, blocks, tail = _blocks(text)
    return head + "".join(b for b in blocks if not b.startswith(f"  - key: {key}\n")) + tail


def test_first_sync_creates_ladder_and_sets_start_current():
    plan = _plan(LadderRowsView(exists=False, milestones=()), FIXTURE)
    assert plan.create_ladder is True
    assert plan.delete_ladder is False
    assert plan.parse_error is None
    assert plan.set_current == "household"
    assert [u["key"] for u in plan.upserts] == list(KEYS)
    assert [u["ordinal"] for u in plan.upserts] == [0, 1, 2, 3]
    assert all(u["state"] == "upcoming" for u in plan.upserts)
    assert plan.remove_keys == () and plan.revive_keys == ()
    f = plan.ladder_fields
    assert f["source_path"] == MILESTONES_PATH
    assert f["source_sha"] == "sha1"
    assert f["title"] == "Launch readiness"
    assert f["spec_path"].endswith("spec.md")
    assert f["definition"]["start"] == "household"
    assert f["parse_error"] is None and f["parse_error_sha"] is None
    friendly = plan.upserts[1]
    assert friendly["epics"] == [f"{REPO}#20192"]
    assert {"name": "Kiosk", "off": True, "note": "not yet"} in friendly["platforms"]
    assert [c["key"] for c in friendly["gate"]][:2] == ["setup", "week-two"]


def test_parse_error_keeps_last_good():
    plan = _plan(_rows(), "version: 1\ntitle: x\nbogus: 1\n", sha="badsha")
    assert plan.upserts == ()
    assert plan.remove_keys == () and plan.revive_keys == ()
    assert plan.set_current is None
    assert plan.create_ladder is False
    assert plan.parse_error is not None and "bogus" in plan.parse_error
    assert plan.ladder_fields["parse_error"] == plan.parse_error
    assert plan.ladder_fields["parse_error_sha"] == "badsha"
    # last good definition and its commit stay in place
    assert "definition" not in plan.ladder_fields
    assert "source_sha" not in plan.ladder_fields


def test_parse_error_on_first_sync_creates_ladder_with_error():
    plan = _plan(LadderRowsView(exists=False, milestones=()), "{{not yaml", sha="badsha")
    assert plan.create_ladder is True
    assert plan.upserts == ()
    assert plan.set_current is None
    assert plan.ladder_fields["parse_error_sha"] == "badsha"


def test_removed_key_marks_removed_and_current_survives():
    plan = _plan(_rows(current="friendly"), _without(FIXTURE, "friendly"))
    assert plan.parse_error is None
    assert plan.remove_keys == ("friendly",)
    assert plan.set_current is None
    assert [u["key"] for u in plan.upserts] == ["household", "community", "public"]
    assert all("state" not in u for u in plan.upserts)


def test_returned_key_is_revived():
    plan = _plan(_rows(removed=("public",)), FIXTURE)
    assert plan.revive_keys == ("public",)
    assert plan.remove_keys == ()


def test_already_removed_key_is_not_removed_again():
    rows = _rows(removed=("friendly",))
    plan = _plan(rows, _without(FIXTURE, "friendly"))
    assert plan.remove_keys == ()


def test_reorder_changes_ordinal_only():
    head, blocks, tail = _blocks(FIXTURE)
    blocks[2], blocks[3] = blocks[3], blocks[2]
    reordered = head + "".join(blocks) + tail

    before = {u["key"]: u for u in _plan(_rows(), FIXTURE).upserts}
    after = {u["key"]: u for u in _plan(_rows(), reordered).upserts}
    assert after["public"]["ordinal"] == 2 and after["community"]["ordinal"] == 3
    for key in KEYS:
        a = dict(after[key])
        b = dict(before[key])
        a.pop("ordinal")
        b.pop("ordinal")
        assert a == b
        assert "state" not in a
    plan = _plan(_rows(), reordered)
    assert plan.set_current is None and plan.remove_keys == () and plan.revive_keys == ()


def test_start_ignored_after_creation():
    plan = _plan(_rows(current=None), FIXTURE)
    assert plan.create_ladder is False
    assert plan.set_current is None


def test_file_removed_sets_error_keeps_rows():
    plan = _plan(_rows(), None, sha="headsha")
    assert plan.delete_ladder is False
    assert plan.create_ladder is False
    assert plan.parse_error == "definition file removed"
    assert plan.ladder_fields["parse_error"] == "definition file removed"
    assert plan.ladder_fields["parse_error_sha"] == "headsha"
    assert plan.upserts == () and plan.remove_keys == () and plan.revive_keys == ()
    assert plan.set_current is None


def test_file_absent_and_no_ladder_is_noop():
    plan = _plan(LadderRowsView(exists=False, milestones=()), None)
    assert plan.is_noop
    assert plan.create_ladder is False and plan.ladder_fields == {}
    assert plan.parse_error is None
