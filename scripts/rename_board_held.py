#!/usr/bin/env python3
"""ONE-SHOT: rename each board's Status option "Needs human" to "Held" IN PLACE.

Spec: docs/superpowers/specs/2026-09-18-hold-ladder-design.md §9. The code produces
``Held`` (``server/pm/desired_state.STATUS_HELD``); before the fleet rename, it also
READ both spellings so a board that had not been renamed yet kept working. This
script closed that window, once per fleet — the read-alias support has since been
dropped from ``desired_state`` (#2015), so a board this script has not yet reached
is now a genuine vocabulary mismatch, not a tolerated rollout state:

    python scripts/rename_board_held.py                    # dry run: print the plan
    python scripts/rename_board_held.py --project viberpit # one board
    python scripts/rename_board_held.py --apply            # perform it

IN PLACE is the whole point. ``updateProjectV2Field`` REPLACES a single-select's
option set, so everything about the set that is not resent is destroyed:

* an input entry without an ``id`` is a NEW option even when the name matches, so
  a names-only rewrite regenerates every option id and blanks the Status of every
  card parked on those columns (probed live on a scratch board, #987);
* ``color``/``description`` are required members of the input, so an option
  resent without them is repainted gray with its description erased;
* an option that is not in the input at all is DELETED, cards and all — which is
  why the set this script resends is read LIVE from the board at plan time, never
  from ``GhProject.field_cache`` (a mirror that lags by up to a ``pm_sync`` tick,
  or by however long the loop has been stopped).

So each option goes back with its own id, colour, description and position, and
exactly one name changes. The mutation is built by the reconciler's own
``build_update_field_options``, the board is read with the mirror's own
query/parse helpers, and the refreshed cache is saved through
``mirror_store.save_field_cache`` — this one-shot cannot drift from the shapes the
reconciler writes every tick (constitution VI).

There is a seconds-wide window between the live plan-building read and the mutation
that resends it: a Status column added on GitHub inside that window is not in the
resent set and is deleted along with its cards, same as the "option not in the
input" case above — there is no compare-and-swap against the field's state. Fine
for a one-shot run when nobody is editing columns (the operator is told to run it
that way); if this script is ever reached for outside a one-time fleet migration,
that assumption needs re-checking first.

Then it CHECKS: after the rename the board is re-read and every option id must be
the one that went in, with "Held" carrying the legacy option's id. A rename that
silently regenerated ids is the one catastrophic outcome (every card in every
column blanked), so it fails loudly instead of saving a cache that looks healthy.

Statuses printed per board:

* ``rename``   — the board carries the legacy option and not "Held": renameable.
* ``done``     — nothing to rename (already "Held", or no held column at all yet,
  which the reconciler's field bootstrap adds as "Held").
* ``conflict`` — the board needs a human before anything is written: BOTH
  spellings exist (the cards must be moved off the old column and the column
  deleted — this script will not merge two columns, because
  ``updateProjectV2Field`` cannot do that without dropping one id and the cards on
  it), or the Status field has no usable id, or two options share a name, or the
  live read failed. Any conflict exits 1.

Auth/DB, the same way the other one-shots resolve them: the board token comes
from the environment (a classic PAT with ``project``+``repo`` scopes — the only
credential verified against user-owned Projects v2), with the ambient
``GITHUB_TOKEN``/``GH_TOKEN`` stripped from the ``gh`` child env so the token in
play is always the explicit one. The env name is spelled with the current brand
prefix; ``mirror_legacy_env()`` (run when ``server.config`` is imported) fills it
from the deployed legacy-prefixed name, so an operator's existing env file works
untouched and this file does not re-spell the old prefix. The database URL comes
from the app settings, but only when it was set explicitly: the settings default
would silently create an empty local SQLite file, and a run against the wrong
database would rename nothing and report every board as ``done``.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from server.pm.desired_state import STATUS_HELD  # noqa: E402 -- repo root added above
from server.pm.gh_mutations import (  # noqa: E402
    build_update_field_options, payload_from_gh)
from server.pm.gh_projects import (  # noqa: E402
    build_project_fields_query, parse_field_options, parse_project_fields)

#: The pre-rename spelling this one-shot looks for. Read-alias support for it was
#: dropped from ``desired_state`` (spec 2026-09-18 §9) once the fleet rename landed,
#: so this script — whose whole job is finding boards that still carry it — keeps
#: its own literal rather than a production import that no longer exists.
LEGACY_STATUS_NEEDS_HUMAN = "Needs human"

#: Canonical spelling of the board-token env name. The legacy-prefixed name an
#: operator's env file actually carries is mirrored onto this one at import.
TOKEN_ENV = "VIBERPIT_GH_PROJECT_TOKEN"
#: Same mirroring, for the explicit-database guard below.
DB_URL_ENV = "VIBERPIT_DB_URL"

#: The Status field's name on every board (the built-in single select).
STATUS_FIELD = "Status"

RENAME, DONE, CONFLICT = "rename", "done", "conflict"

#: A GraphQL runner: takes one document, returns the parsed payload. Injected so
#: the planner, the mutation and the verification can be exercised without a network.
GhRunner = Callable[[str], Awaitable[dict]]


class OptionIdsChanged(RuntimeError):
    """The re-read says the rename did not preserve the option ids — cards are at risk."""


class RefreshFailed(RuntimeError):
    """The rename LANDED; reading it back or caching it did not. Re-runnable."""


@dataclass(frozen=True)
class Board:
    """One mirrored Projects v2 board: where to read it and where to cache it."""

    slug: str
    node_id: str
    db_id: str


@dataclass(frozen=True)
class Option:
    """One single-select option exactly as the board reports it."""

    id: str
    name: str
    color: str = ""
    description: str = ""


@dataclass(frozen=True)
class StatusField:
    """A board's live Status field: its id and its options IN BOARD ORDER.

    ``duplicate_names`` is set when two options share a name. The field cache is a
    ``{name: id}`` map, so it cannot represent that — and a writer rebuilding the
    set from it would silently delete one of them."""

    field_id: str | None
    options: tuple[Option, ...] = ()
    duplicate_names: bool = False
    error: str = ""


@dataclass(frozen=True)
class Rename:
    """What to do with one board's Status field.

    ``options`` is the live option set in the board's own order; order, ids and
    attributes are all load-bearing, because the mutation resends the whole set.
    ``renamed_index`` points at the legacy entry (-1 when there is nothing to
    rename)."""

    project_slug: str
    status: str
    field_id: str | None = None
    options: tuple[Option, ...] = ()
    renamed_index: int = -1
    reason: str = ""

    @property
    def option_id(self) -> str | None:
        """The id of the option being renamed — the id that must survive."""
        if self.renamed_index < 0:
            return None
        return self.options[self.renamed_index].id

    @property
    def new_names(self) -> list[str]:
        """The board's option names with the legacy one replaced, order kept."""
        names = [o.name for o in self.options]
        if self.renamed_index >= 0:
            names[self.renamed_index] = STATUS_HELD
        return names

    @property
    def option_ids(self) -> dict[str, str]:
        """NEW name -> the existing option id it must keep.

        Keyed by the new name because that is what ``build_update_field_options``
        looks each option up by: mapping "Held" to the legacy option's id is
        precisely what turns a delete-and-create into a rename. It is also what
        the board must report back afterwards, option for option."""
        return {new: o.id for new, o in zip(self.new_names, self.options) if o.id}

    @property
    def option_attrs(self) -> dict[str, dict[str, str]]:
        """NEW name -> the colour and description it must keep."""
        return {new: {"color": o.color, "description": o.description}
                for new, o in zip(self.new_names, self.options)}


def read_status_field(payload: dict) -> StatusField:
    """The live Status field out of one ``build_project_fields_query`` payload."""
    cache = parse_project_fields(payload)
    raw = parse_field_options(payload, STATUS_FIELD)
    options = tuple(Option(o["id"], o["name"], o["color"], o["description"]) for o in raw)
    cached_names = (cache.get(STATUS_FIELD) or {}).get("options") or {}
    return StatusField(
        field_id=(cache.get(STATUS_FIELD) or {}).get("field_id") or None,
        options=options,
        # The cache collapses same-named options; a shorter map means it did.
        duplicate_names=len(cached_names) != len(options),
    )


def plan_renames(status_by_project: Mapping[str, StatusField]) -> list[Rename]:
    """Classify every board from its LIVE Status field. Pure: no network, no database."""
    plans: list[Rename] = []
    for slug in sorted(status_by_project):
        field = status_by_project[slug]
        names = [o.name for o in field.options]
        index = (names.index(LEGACY_STATUS_NEEDS_HUMAN)
                 if LEGACY_STATUS_NEEDS_HUMAN in names else -1)
        if field.error:
            status, reason = CONFLICT, f"could not read the board: {field.error}"
        elif field.duplicate_names:
            # Rebuilding the set would keep one of the twins and delete the other.
            status, reason = CONFLICT, "two Status options share a name; fix the board first"
        elif index < 0:
            status, reason = DONE, ""
        elif STATUS_HELD in names:
            status, reason = CONFLICT, "both columns exist; move the cards off the legacy one first"
        elif not field.field_id:
            # ``_render`` refuses to write against a field with no id, and so does
            # this: the mutation has nothing to address.
            status, reason = CONFLICT, "Status field id missing; re-read the board"
        else:
            status, reason = RENAME, ""
        plans.append(Rename(slug, status, field.field_id, field.options,
                            index if status != DONE else -1, reason))
    return plans


def _data_or_raise(payload: dict, what: str) -> dict:
    errors = [str((e or {}).get("message") or e) for e in (payload.get("errors") or [])]
    if errors:
        raise RuntimeError(f"{what} failed: {errors[0]}")
    return payload


async def read_board(gh: GhRunner, board: Board) -> StatusField:
    """Read one board's Status field live. A failed read becomes a ``conflict``."""
    try:
        payload = _data_or_raise(await gh(build_project_fields_query(board.node_id)),
                                 f"fields read on {board.slug}")
    except Exception as exc:
        return StatusField(field_id=None, error=str(exc))
    field = read_status_field(payload)
    if not field.field_id:
        return StatusField(field_id=None, error="no Status field returned")
    return field


def verify_ids(rename: Rename, fresh: StatusField) -> None:
    """Raise unless the board now reports EXACTLY the ids that were sent.

    This is the check that separates "we believe no card was lost" from "the board
    said so": a regenerated id means every card in that column came back with a
    null Status, and a cache saved over that failure looks perfectly healthy."""
    got = {o.name: o.id for o in fresh.options}
    expected = rename.option_ids
    if len(fresh.options) != len(rename.options):
        raise OptionIdsChanged("option ids changed — cards may be blanked, inspect the board")
    if got.get(STATUS_HELD) != rename.option_id:
        raise OptionIdsChanged("option ids changed — cards may be blanked, inspect the board")
    for name, oid in expected.items():
        if got.get(name) != oid:
            raise OptionIdsChanged("option ids changed — cards may be blanked, inspect the board")


async def _save_to_db(board: Board, cache: dict) -> None:
    # ``server.pm.mirror_store`` and ``server.automations`` import each other, and
    # the cycle resolves in exactly ONE direction: automations first, because
    # ``server.pm.dep_plan`` only needs a SUBMODULE of a half-initialised
    # ``server.automations``, whereas ``pm_sync`` needs a NAME out of a
    # half-initialised ``mirror_store`` and gets an ImportError. The app and the
    # test suite take that direction by accident (they import the app first); a
    # script is the entry point, so it takes it deliberately.
    import server.automations  # noqa: F401 -- import ORDER, see above
    from server.db import db_session
    from server.pm.mirror_store import save_field_cache

    async with db_session() as db:
        await db.run_sync(lambda s: save_field_cache(s, board.db_id, cache))


async def apply_rename(
    gh: GhRunner, rename: Rename, board: Board,
    *, save: Callable[[Board, dict], Awaitable[None]] = _save_to_db,
) -> dict[str, str]:
    """Perform one rename, verify it, and refresh the board's cached fields.

    Returns the refreshed ``Status`` options (name -> id), which is also what the
    caller prints: the proof that the ids are the ones that went in.

    Failure modes are distinguished because they need different human responses:
    a failed mutation changed nothing; :class:`OptionIdsChanged` means go look at
    the board; :class:`RefreshFailed` means the rename LANDED and only the cache is
    stale, so re-running ``--apply`` is the fix (it resends identical ids, and both
    spellings read and write fine until the next ``pm_sync`` tick catches up)."""
    if rename.status != RENAME or not rename.field_id:
        raise ValueError(f"{rename.project_slug}: not a renameable plan ({rename.status})")
    doc = build_update_field_options(rename.field_id, rename.new_names,
                                     option_ids=rename.option_ids,
                                     option_attrs=rename.option_attrs)
    _data_or_raise(await gh(doc), f"rename on {rename.project_slug}")
    # Re-read the board rather than trusting the mutation's echo: the cache the
    # reconciler writes through must be what GitHub actually reports.
    try:
        payload = _data_or_raise(await gh(build_project_fields_query(board.node_id)),
                                 f"field re-read on {rename.project_slug}")
        fresh = read_status_field(payload)
        if not fresh.field_id:
            raise RuntimeError("the re-read returned no Status field")
    except Exception as exc:
        raise RefreshFailed(str(exc)) from exc
    # Verified BEFORE the save, and never downgraded to a refresh failure: this one
    # is not "re-run it", it is "stop and look at the board".
    verify_ids(rename, fresh)
    try:
        await save(board, parse_project_fields(payload))
    except Exception as exc:
        raise RefreshFailed(f"cache save failed: {exc}") from exc
    return {o.name: o.id for o in fresh.options}


# --------------------------------------------------------------------------- #
# gh shell                                                                     #
# --------------------------------------------------------------------------- #

def _gh_env(token: str | None) -> dict[str, str]:
    """The child's env: ambient GitHub tokens stripped, the board token injected."""
    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN")}
    if token:
        env["GITHUB_TOKEN"] = token
    return env


def _gh(args: Sequence[str], token: str | None) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True, env=_gh_env(token))


def gh_runner(token: str | None) -> GhRunner:
    async def run(doc: str) -> dict:
        proc = await asyncio.to_thread(_gh, ["api", "graphql", "-f", f"query={doc}"], token)
        # partial_ok: the GraphQL errors are what ``_data_or_raise`` reports, and
        # they are more use than gh's exit code.
        return payload_from_gh(proc, partial_ok=True)
    return run


# --------------------------------------------------------------------------- #
# database                                                                     #
# --------------------------------------------------------------------------- #

async def load_boards(slug: str | None = None) -> list[Board]:
    """Every mirrored board (optionally one). Identity only — the option set is
    read live from GitHub, never from the cached mirror.

    A row with no ``gh_node_id`` (a ``Project`` never linked to a live board) is
    skipped rather than silently dropped: with ``--project`` this is the only
    thing standing between a real "board not found" and the misleading "no
    mirrored boards found" the caller prints when the filtered list comes back
    empty."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import GhProject, Project

    async with db_session() as db:
        rows = (await db.execute(
            select(Project.slug, GhProject.gh_node_id, GhProject.id)
            .select_from(GhProject)
            .join(Project, Project.id == GhProject.project_id)
        )).all()
    boards = []
    for slug_, node_id, db_id in rows:
        if not slug_ or not node_id:
            print(f"skip: {slug_ or db_id} has no gh_node_id, not a live board", file=sys.stderr)
            continue
        boards.append(Board(str(slug_), str(node_id), str(db_id)))
    if slug:
        boards = [b for b in boards if b.slug == slug]
    return sorted(boards, key=lambda b: b.slug)


# --------------------------------------------------------------------------- #
# entry point                                                                  #
# --------------------------------------------------------------------------- #

def print_table(plans: Sequence[Rename]) -> None:
    width = max([len(p.project_slug) for p in plans] + [4])
    print(f"{'slug'.ljust(width)}  {'status'.ljust(8)}  option-id")
    for plan in plans:
        line = f"{plan.project_slug.ljust(width)}  {plan.status.ljust(8)}  {plan.option_id or '-'}"
        print(f"{line}  ({plan.reason})" if plan.reason else line)


async def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="perform the renames (without this, only print the plan)")
    ap.add_argument("--project", metavar="SLUG", help="restrict to one product's board")
    args = ap.parse_args(argv)

    if not os.environ.get(DB_URL_ENV):
        # The settings default would create an empty local SQLite file, in which
        # there are no boards at all — a silent all-clear on the wrong database.
        print(f"error: {DB_URL_ENV} must be set (the live database)", file=sys.stderr)
        return 2
    from server.config import get_settings
    from server.db import init_db
    url = get_settings().db_url
    print(f"database: {url.rsplit('@', 1)[-1]}")  # never the credentials half
    await init_db(url)

    boards = {b.slug: b for b in await load_boards(args.project)}
    if not boards:
        print("no mirrored boards found" + (f" for {args.project}" if args.project else ""))
        return 2

    token = os.environ.get(TOKEN_ENV) or ""
    if not token:
        # Needed by the DRY RUN too: the plan is built from a live read of each
        # board, because the cached option set may be missing a column a human
        # added since the last sync — and resending the set would delete it.
        print(f"error: {TOKEN_ENV} must be set to read the boards", file=sys.stderr)
        return 2
    gh = gh_runner(token)

    live = {slug: await read_board(gh, board) for slug, board in boards.items()}
    plans = plan_renames(live)
    print_table(plans)
    conflicts = sum(1 for p in plans if p.status == CONFLICT)
    renameable = [p for p in plans if p.status == RENAME]

    if not args.apply:
        print(f"\n{len(renameable)} board(s) to rename, {conflicts} conflict(s) — "
              "re-run with --apply to perform them")
        return 1 if conflicts else 0

    failures = 0
    for plan in renameable:
        board = boards[plan.project_slug]
        try:
            options = await apply_rename(gh, plan, board)
        except OptionIdsChanged as exc:
            failures += 1
            print(f"FAIL  {plan.project_slug}: {exc}")
            continue
        except RefreshFailed as exc:
            failures += 1
            print(f"RENAMED  {plan.project_slug} but the cache refresh failed ({exc}) — "
                  "re-run --apply to refresh")
            continue
        except Exception as exc:  # one board's failure must not abandon the rest
            failures += 1
            print(f"FAIL  {plan.project_slug}  ({exc})")
            continue
        rendered = ", ".join(f"{name}={oid}" for name, oid in options.items())
        print(f"DONE  {plan.project_slug}  Status: {rendered}")
    print(f"\n{len(renameable)} rename(s), {failures} failed, {conflicts} conflict(s)")
    return 1 if (failures or conflicts) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
