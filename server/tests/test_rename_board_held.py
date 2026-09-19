"""The one-shot board rename "Needs human" -> "Held" (spec 2026-09-18 §9)."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

from server.pm.desired_state import LEGACY_STATUS_NEEDS_HUMAN, STATUS_HELD

SCRIPT = Path("scripts/rename_board_held.py")


def _module():
    spec = importlib.util.spec_from_file_location("rename_board_held", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Registered before exec: @dataclass resolves annotations through
    # ``sys.modules[cls.__module__]``, which is None for an unregistered module.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _module()


def _cache(*options: tuple[str, str], field_id: str | None = "F-status") -> dict:
    return {"Status": {"field_id": field_id, "options": dict(options)}}


LEGACY_BOARD = _cache(("Backlog", "o-back"), (LEGACY_STATUS_NEEDS_HUMAN, "o-held"),
                      ("Done", "o-done"))
BOTH_BOARD = _cache(("Backlog", "o-back"), (LEGACY_STATUS_NEEDS_HUMAN, "o-legacy"),
                    (STATUS_HELD, "o-held"))
HELD_BOARD = _cache(("Backlog", "o-back"), (STATUS_HELD, "o-held"))


def test_plan_classifies_each_board():
    """Legacy only renames; both spellings is a conflict; already-Held is done."""
    plans = {p.project_slug: p for p in mod.plan_renames(
        {"legacy": LEGACY_BOARD, "both": BOTH_BOARD, "held": HELD_BOARD})}

    assert plans["legacy"].status == "rename"
    assert plans["legacy"].field_id == "F-status"
    assert plans["legacy"].renamed_index == 1
    assert plans["legacy"].option_id == "o-held"

    assert plans["both"].status == "conflict"
    assert plans["both"].reason  # a human has to move the cards first
    assert plans["held"].status == "done"
    assert plans["held"].option_id is None


def test_plan_conflicts_when_the_status_field_has_no_id():
    """A cache entry with no field id cannot be written against — never renamed."""
    (plan,) = mod.plan_renames({"stale": _cache(
        (LEGACY_STATUS_NEEDS_HUMAN, "o-held"), field_id=None)})
    assert plan.status == "conflict"
    assert "field id" in plan.reason


def test_plan_is_done_for_a_board_with_no_status_options_at_all():
    """Nothing to rename: the reconciler's bootstrap adds the column as "Held"."""
    plans = mod.plan_renames({"empty": {}, "fieldless": {"Status": {}}})
    assert [p.status for p in plans] == ["done", "done"]


def test_rename_keeps_every_option_id_and_the_board_order():
    """The rename is the SAME ids with one name changed, in the board's own order."""
    (plan,) = mod.plan_renames({"legacy": LEGACY_BOARD})
    assert plan.new_names == ["Backlog", STATUS_HELD, "Done"]
    # Keyed by the NEW name: "Held" carries the legacy option's id, which is what
    # makes updateProjectV2Field rename the column instead of replacing it.
    assert plan.option_ids == {"Backlog": "o-back", STATUS_HELD: "o-held", "Done": "o-done"}


class FakeGh:
    """Records each GraphQL document and replies with a canned payload."""

    def __init__(self, *payloads: dict) -> None:
        self.docs: list[str] = []
        self._payloads = list(payloads)

    async def __call__(self, doc: str) -> dict:
        self.docs.append(doc)
        return self._payloads.pop(0)


def _fields_payload(*options: tuple[str, str]) -> dict:
    return {"data": {"node": {"fields": {"nodes": [{
        "id": "F-status", "name": "Status",
        "options": [{"id": oid, "name": name} for name, oid in options]}]}}}}


def _board():
    return mod.Board(slug="legacy", node_id="PVT_board", db_id="db-1",
                     field_cache=LEGACY_BOARD)


def _apply(gh):
    (plan,) = mod.plan_renames({"legacy": LEGACY_BOARD})
    saved: list[tuple[str, dict]] = []

    async def save(board, cache):
        saved.append((board.db_id, cache))

    options = asyncio.run(mod.apply_rename(gh, plan, _board(), save=save))
    return options, saved


def test_apply_sends_every_existing_id_with_the_renamed_name_and_saves_the_refresh():
    gh = FakeGh({"data": {"updateProjectV2Field": {"projectV2Field": {"id": "F-status"}}}},
                _fields_payload(("Backlog", "o-back"), (STATUS_HELD, "o-held"),
                                ("Done", "o-done")))
    options, saved = _apply(gh)

    mutation = gh.docs[0]
    assert "updateProjectV2Field" in mutation
    assert 'fieldId: "F-status"' in mutation
    # Every option goes back with the id it already had — the renamed one included,
    # which is the whole point: a fresh id would blank every card in that column.
    for name, oid in (("Backlog", "o-back"), (STATUS_HELD, "o-held"), ("Done", "o-done")):
        assert f'{{id: "{oid}", name: "{name}"' in mutation
    assert LEGACY_STATUS_NEEDS_HUMAN not in mutation
    # Then the board is re-read and the refreshed cache saved for that board row.
    assert "PVT_board" in gh.docs[1]
    assert [db_id for db_id, _ in saved] == ["db-1"]
    assert saved[0][1]["Status"]["options"] == {
        "Backlog": "o-back", STATUS_HELD: "o-held", "Done": "o-done"}
    assert options == saved[0][1]["Status"]["options"]


def test_apply_raises_on_a_graphql_error_and_saves_nothing():
    gh = FakeGh({"errors": [{"message": "Resource not accessible"}]})
    with pytest.raises(RuntimeError, match="Resource not accessible"):
        _apply(gh)
    assert len(gh.docs) == 1  # never re-read the board after a failed mutation


def test_apply_raises_when_the_refresh_returns_no_status_field():
    """A refresh that came back empty must not be saved over a good cache."""
    gh = FakeGh({"data": {"updateProjectV2Field": {"projectV2Field": {"id": "F-status"}}}},
                {"data": {"node": {"fields": {"nodes": []}}}})
    with pytest.raises(RuntimeError, match="no Status field"):
        _apply(gh)


def test_script_runs_standalone_and_refuses_an_implicit_database():
    """Imported as the ENTRY module, not alongside an already-loaded server.

    ``server.pm.mirror_store`` and ``server.automations`` import each other, so a
    top-level import of the former resolves only when something else loaded the
    chain first — which a one-shot script never has. Running the file is the only
    check that catches it; in-process tests inherit the test suite's imports.

    The run also asserts the explicit-database guard: without the env var the
    settings default would build an empty SQLite file and report every board as
    already renamed."""
    import os
    import subprocess

    env = {k: v for k, v in os.environ.items()
           if not k.endswith("_DB_URL")}
    proc = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True,
                          env=env, cwd=str(SCRIPT.resolve().parents[1]))
    assert proc.returncode == 2, proc.stderr
    assert mod.DB_URL_ENV in proc.stderr


def test_apply_refuses_a_plan_that_is_not_a_rename():
    (plan,) = mod.plan_renames({"both": BOTH_BOARD})
    with pytest.raises(ValueError):
        asyncio.run(mod.apply_rename(FakeGh(), plan, _board()))
