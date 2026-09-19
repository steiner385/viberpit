"""The one-shot board rename "Needs human" -> "Held" (spec 2026-09-18 §9)."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

from server.pm.desired_state import LEGACY_STATUS_NEEDS_HUMAN, STATUS_HELD

#: Absolute, so collection does not depend on pytest's cwd (this module imports the
#: script at import time, so a relative path errors the whole file, not one test).
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "rename_board_held.py"


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


def _fields_payload(*options: tuple[str, str], field_id: str | None = "F-status",
                    colors: dict[str, str] | None = None,
                    descriptions: dict[str, str] | None = None) -> dict:
    """A ``build_project_fields_query`` response for one board's Status field."""
    node: dict = {"name": "Status", "options": [
        {"id": oid, "name": name, "color": (colors or {}).get(name, "GRAY"),
         "description": (descriptions or {}).get(name, "")}
        for name, oid in options]}
    if field_id:
        node["id"] = field_id
    return {"data": {"node": {"fields": {"nodes": [node]}}}}


LEGACY_PAYLOAD = _fields_payload(
    ("Backlog", "o-back"), (LEGACY_STATUS_NEEDS_HUMAN, "o-held"), ("Done", "o-done"),
    colors={"Backlog": "BLUE", LEGACY_STATUS_NEEDS_HUMAN: "ORANGE", "Done": "PURPLE"},
    descriptions={LEGACY_STATUS_NEEDS_HUMAN: "waiting on a person"})
BOTH_PAYLOAD = _fields_payload(("Backlog", "o-back"), (LEGACY_STATUS_NEEDS_HUMAN, "o-legacy"),
                               (STATUS_HELD, "o-held"))
HELD_PAYLOAD = _fields_payload(("Backlog", "o-back"), (STATUS_HELD, "o-held"))

LEGACY = mod.read_status_field(LEGACY_PAYLOAD)
BOTH = mod.read_status_field(BOTH_PAYLOAD)
HELD = mod.read_status_field(HELD_PAYLOAD)


def test_read_status_field_keeps_order_ids_and_attributes():
    """The plan is built from the LIVE board, attributes included — an option resent
    without its colour/description comes back gray and blank."""
    assert [(o.name, o.id) for o in LEGACY.options] == [
        ("Backlog", "o-back"), (LEGACY_STATUS_NEEDS_HUMAN, "o-held"), ("Done", "o-done")]
    assert LEGACY.field_id == "F-status"
    assert LEGACY.options[1].color == "ORANGE"
    assert LEGACY.options[1].description == "waiting on a person"
    assert LEGACY.duplicate_names is False


def test_plan_classifies_each_board():
    """Legacy only renames; both spellings is a conflict; already-Held is done."""
    plans = {p.project_slug: p for p in mod.plan_renames(
        {"legacy": LEGACY, "both": BOTH, "held": HELD})}

    assert plans["legacy"].status == "rename"
    assert plans["legacy"].field_id == "F-status"
    assert plans["legacy"].renamed_index == 1
    assert plans["legacy"].option_id == "o-held"

    assert plans["both"].status == "conflict"
    assert plans["both"].reason  # a human has to move the cards first
    assert plans["held"].status == "done"
    assert plans["held"].option_id is None


def test_plan_conflicts_when_the_status_field_has_no_id():
    """A field with no id cannot be written against — never renamed."""
    field = mod.read_status_field(
        _fields_payload((LEGACY_STATUS_NEEDS_HUMAN, "o-held"), field_id=None))
    (plan,) = mod.plan_renames({"stale": field})
    assert plan.status == "conflict"
    assert "field id" in plan.reason


def test_plan_conflicts_on_duplicate_option_names():
    """Two options sharing a name collapse in the ``{name: id}`` cache, so rebuilding
    the set would delete one of them and blank its cards. Refuse the board."""
    field = mod.read_status_field(_fields_payload(
        ("Backlog", "o-back"), (LEGACY_STATUS_NEEDS_HUMAN, "o-held"),
        (LEGACY_STATUS_NEEDS_HUMAN, "o-twin")))
    assert field.duplicate_names is True
    (plan,) = mod.plan_renames({"twins": field})
    assert plan.status == "conflict"
    assert "share a name" in plan.reason


def test_plan_conflicts_when_the_live_read_failed():
    """A board we could not read is never planned from a stale cache."""
    (plan,) = mod.plan_renames(
        {"unreachable": mod.StatusField(field_id=None, error="Bad credentials")})
    assert plan.status == "conflict"
    assert "Bad credentials" in plan.reason


def test_plan_is_done_for_a_board_with_no_status_options_at_all():
    """Nothing to rename: the reconciler's bootstrap adds the column as "Held"."""
    plans = mod.plan_renames({"empty": mod.read_status_field({}),
                              "fieldless": mod.read_status_field(_fields_payload())})
    assert [p.status for p in plans] == ["done", "done"]


def test_rename_keeps_every_option_id_attribute_and_the_board_order():
    """The rename is the SAME ids and attributes with one name changed, in order."""
    (plan,) = mod.plan_renames({"legacy": LEGACY})
    assert plan.new_names == ["Backlog", STATUS_HELD, "Done"]
    # Keyed by the NEW name: "Held" carries the legacy option's id, which is what
    # makes updateProjectV2Field rename the column instead of replacing it.
    assert plan.option_ids == {"Backlog": "o-back", STATUS_HELD: "o-held", "Done": "o-done"}
    assert plan.option_attrs[STATUS_HELD] == {"color": "ORANGE",
                                              "description": "waiting on a person"}
    assert plan.option_attrs["Backlog"]["color"] == "BLUE"


class FakeGh:
    """Records each GraphQL document and replies with a canned payload."""

    def __init__(self, *payloads: dict) -> None:
        self.docs: list[str] = []
        self._payloads = list(payloads)

    async def __call__(self, doc: str) -> dict:
        self.docs.append(doc)
        return self._payloads.pop(0)


MUTATION_OK = {"data": {"updateProjectV2Field": {"projectV2Field": {"id": "F-status"}}}}
RENAMED_PAYLOAD = _fields_payload(
    ("Backlog", "o-back"), (STATUS_HELD, "o-held"), ("Done", "o-done"),
    colors={"Backlog": "BLUE", STATUS_HELD: "ORANGE", "Done": "PURPLE"})


def _board():
    return mod.Board(slug="legacy", node_id="PVT_board", db_id="db-1")


def _apply(gh, save=None):
    (plan,) = mod.plan_renames({"legacy": LEGACY})
    saved: list[tuple[str, dict]] = []

    async def _record(board, cache):
        saved.append((board.db_id, cache))

    options = asyncio.run(mod.apply_rename(gh, plan, _board(), save=save or _record))
    return options, saved


def test_read_board_turns_a_failed_read_into_a_conflict():
    """No board is planned from the DB cache — an unreadable board is reported, not guessed."""
    gh = FakeGh({"errors": [{"message": "Bad credentials"}]})
    field = asyncio.run(mod.read_board(gh, _board()))
    assert field.error and "Bad credentials" in field.error
    assert mod.plan_renames({"legacy": field})[0].status == "conflict"


def test_read_board_reads_the_live_option_set():
    gh = FakeGh(LEGACY_PAYLOAD)
    field = asyncio.run(mod.read_board(gh, _board()))
    assert "PVT_board" in gh.docs[0]
    assert [o.name for o in field.options] == [
        "Backlog", LEGACY_STATUS_NEEDS_HUMAN, "Done"]


def test_apply_sends_every_existing_id_and_attribute_with_the_renamed_name():
    gh = FakeGh(MUTATION_OK, RENAMED_PAYLOAD)
    options, saved = _apply(gh)

    mutation = gh.docs[0]
    assert "updateProjectV2Field" in mutation
    assert 'fieldId: "F-status"' in mutation
    # Every option goes back with the id it already had — the renamed one included,
    # which is the whole point: a fresh id would blank every card in that column.
    # And with its colour and description, or the rename repaints the whole board.
    assert '{id: "o-back", name: "Backlog", color: BLUE, description: ""}' in mutation
    assert (f'{{id: "o-held", name: "{STATUS_HELD}", color: ORANGE, '
            'description: "waiting on a person"}') in mutation
    assert '{id: "o-done", name: "Done", color: PURPLE, description: ""}' in mutation
    assert LEGACY_STATUS_NEEDS_HUMAN not in mutation
    # Then the board is re-read and the refreshed cache saved for that board row.
    assert "PVT_board" in gh.docs[1]
    assert [db_id for db_id, _ in saved] == ["db-1"]
    assert saved[0][1]["Status"]["options"] == {
        "Backlog": "o-back", STATUS_HELD: "o-held", "Done": "o-done"}
    assert options == saved[0][1]["Status"]["options"]


def test_apply_fails_loudly_and_saves_nothing_when_the_ids_came_back_changed():
    """The catastrophic mode: regenerated ids mean every card in the column is blank.
    It must never be reported as DONE, and never cached as healthy."""
    regenerated = _fields_payload(("Backlog", "o-back"), (STATUS_HELD, "NEW-id"),
                                  ("Done", "o-done"))
    gh = FakeGh(MUTATION_OK, regenerated)
    with pytest.raises(mod.OptionIdsChanged, match="cards may be blanked"):
        _apply(gh)

    # Another option's id changing is caught by the same check.
    gh = FakeGh(MUTATION_OK, _fields_payload(("Backlog", "MOVED"), (STATUS_HELD, "o-held"),
                                             ("Done", "o-done")))
    with pytest.raises(mod.OptionIdsChanged):
        _apply(gh)


def test_apply_raises_on_a_graphql_error_and_saves_nothing():
    gh = FakeGh({"errors": [{"message": "Resource not accessible"}]})
    with pytest.raises(RuntimeError, match="Resource not accessible"):
        _apply(gh)
    assert len(gh.docs) == 1  # never re-read the board after a failed mutation


def test_a_failed_refresh_reports_that_the_rename_landed():
    """The mutation succeeded: the operator must not read "FAIL" and assume otherwise."""
    gh = FakeGh(MUTATION_OK, {"data": {"node": {"fields": {"nodes": []}}}})
    with pytest.raises(mod.RefreshFailed, match="no Status field"):
        _apply(gh)


def test_a_failed_cache_save_also_reports_that_the_rename_landed():
    async def boom(board, cache):
        raise RuntimeError("database gone")

    gh = FakeGh(MUTATION_OK, RENAMED_PAYLOAD)
    with pytest.raises(mod.RefreshFailed, match="database gone"):
        _apply(gh, save=boom)


def test_verify_ids_accepts_the_set_that_was_sent():
    (plan,) = mod.plan_renames({"legacy": LEGACY})
    mod.verify_ids(plan, mod.read_status_field(RENAMED_PAYLOAD))  # does not raise


@pytest.mark.slow  # spawns an interpreter — that spawn IS what is under test
def test_script_runs_standalone_and_refuses_an_implicit_database():
    """Imported as the ENTRY module, not alongside an already-loaded server.

    ``server.pm.mirror_store`` and ``server.automations`` import each other, so a
    top-level import of the former resolves only when something else loaded the
    chain first — which a one-shot script never has. Running the file is the only
    check that catches it; in-process tests inherit the test suite's imports.

    The run also asserts the explicit-database guard: without the env var the
    settings default would build an empty SQLite file, where there are no boards
    at all."""
    import os
    import subprocess

    env = {k: v for k, v in os.environ.items()
           if not k.endswith("_DB_URL")}
    proc = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True,
                          env=env, cwd=str(SCRIPT.resolve().parents[1]))
    assert proc.returncode == 2, proc.stderr
    assert mod.DB_URL_ENV in proc.stderr


@pytest.mark.slow  # spawns an interpreter — that spawn IS what is under test
def test_the_cache_save_path_imports_from_a_standalone_script():
    """The save's own imports, run with the script as the entry point.

    ``mirror_store`` ↔ ``server.automations`` is a real cycle that resolves in only
    one direction, and the in-process tests inherit the suite's import graph, so
    they cannot see it. Reaching ``db_session``'s "not initialised" complaint means
    every import in ``_save_to_db`` resolved; an ImportError would mean the rename
    lands and the cache save then fails on the live board."""
    import os
    import subprocess

    snippet = (
        "import asyncio, sys; sys.path.insert(0, '.');"
        "import scripts.rename_board_held as m;"
        "board = m.Board('s', 'PVT_1', 'db-1');"
        "print(asyncio.run(m._save_to_db(board, {})))")
    env = {k: v for k, v in os.environ.items() if not k.endswith("_DB_URL")}
    proc = subprocess.run([sys.executable, "-c", snippet], capture_output=True, text=True,
                          env=env, cwd=str(SCRIPT.resolve().parents[1]))
    assert "ImportError" not in proc.stderr, proc.stderr
    assert "Database not initialised" in proc.stderr, proc.stderr


def test_apply_refuses_a_plan_that_is_not_a_rename():
    (plan,) = mod.plan_renames({"both": BOTH})
    with pytest.raises(ValueError):
        asyncio.run(mod.apply_rename(FakeGh(), plan, _board()))
