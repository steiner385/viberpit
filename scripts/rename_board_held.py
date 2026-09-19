#!/usr/bin/env python3
"""ONE-SHOT: rename each board's Status option "Needs human" to "Held" IN PLACE.

Spec: docs/superpowers/specs/2026-09-18-hold-ladder-design.md §9. The code now
produces ``Held`` (``server/pm/desired_state.STATUS_HELD``) and READS both
spellings through ``is_held_status`` so a board that has not been renamed yet
keeps working. This script closes that window, once per fleet:

    python scripts/rename_board_held.py                    # dry run: print the plan
    python scripts/rename_board_held.py --project viberpit # one board
    python scripts/rename_board_held.py --apply            # perform it

IN PLACE is the whole point. ``updateProjectV2Field`` REPLACES a single-select's
option set, and an input entry without an ``id`` is a NEW option even when the
name matches — so a names-only rewrite regenerates every option id and blanks the
Status of every card parked on those columns (probed live on a scratch board,
#987). Every option therefore goes back with the id it already has, the renamed
one included: same id, new name, so each card keeps its column. The mutation is
built by the reconciler's own ``build_update_field_options`` and the refreshed
field cache is read back with the mirror's own query/parse helpers, so this
one-shot cannot drift from the shapes the reconciler writes every tick
(constitution VI).

Statuses printed per board:

* ``rename``   — the board carries the legacy option and not "Held": renameable.
* ``done``     — nothing to rename (already "Held", or no held column at all yet,
  which the reconciler's field bootstrap adds as "Held").
* ``conflict`` — BOTH spellings exist (or the legacy one exists with no usable
  field id). A human must move the cards off the old column and delete it; this
  script will not merge two columns, because ``updateProjectV2Field`` cannot do
  that without dropping one id and the cards on it. Any conflict exits 1.

Auth/DB, the same way the other one-shots resolve them: the board token comes
from the environment (a classic PAT with ``project``+``repo`` scopes — the only
credential verified against user-owned Projects v2), with the ambient
``GITHUB_TOKEN``/``GH_TOKEN`` stripped from the ``gh`` child env so the token in
play is always the explicit one. The env name is spelled with the current brand
prefix; ``mirror_legacy_env()`` (run when ``server.config`` is imported) fills it
from the deployed legacy-prefixed name, so an operator's existing env file works
untouched and this file does not re-spell the old prefix. The database URL comes
from the app settings, but only when it was set explicitly: the settings default
would silently create an empty local SQLite file and report every board as
``done``.
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

from server.pm.desired_state import (  # noqa: E402 -- repo root added above
    LEGACY_STATUS_NEEDS_HUMAN, STATUS_HELD)
from server.pm.gh_mutations import (  # noqa: E402
    build_update_field_options, payload_from_gh)
from server.pm.gh_projects import (  # noqa: E402
    build_project_fields_query, parse_project_fields)

#: Canonical spelling of the board-token env name. The legacy-prefixed name an
#: operator's env file actually carries is mirrored onto this one at import.
TOKEN_ENV = "VIBERPIT_GH_PROJECT_TOKEN"
#: Same mirroring, for the explicit-database guard below.
DB_URL_ENV = "VIBERPIT_DB_URL"

#: The Status field's name on every board (the built-in single select).
STATUS_FIELD = "Status"

RENAME, DONE, CONFLICT = "rename", "done", "conflict"

#: A GraphQL runner: takes one document, returns the parsed payload. Injected so
#: the planner and the mutation builder can be exercised without a network.
GhRunner = Callable[[str], Awaitable[dict]]


@dataclass(frozen=True)
class Board:
    """One mirrored Projects v2 board, as the rename needs it."""

    slug: str
    node_id: str
    db_id: str
    field_cache: dict


@dataclass(frozen=True)
class Rename:
    """What to do with one board's Status field.

    ``options`` is the board's option set in ITS OWN ORDER as ``(name, id)`` —
    order and ids are both load-bearing, since the mutation resends the whole set
    and an option that comes back without its id is a new, empty column.
    ``renamed_index`` points at the legacy entry (-1 when there is nothing to
    rename)."""

    project_slug: str
    status: str
    field_id: str | None = None
    options: tuple[tuple[str, str], ...] = ()
    renamed_index: int = -1
    reason: str = ""

    @property
    def option_id(self) -> str | None:
        """The id of the option being renamed — the id that must survive."""
        if self.renamed_index < 0:
            return None
        return self.options[self.renamed_index][1]

    @property
    def new_names(self) -> list[str]:
        """The board's option names with the legacy one replaced, order kept."""
        names = [name for name, _ in self.options]
        if self.renamed_index >= 0:
            names[self.renamed_index] = STATUS_HELD
        return names

    @property
    def option_ids(self) -> dict[str, str]:
        """NEW name -> the existing option id it must keep.

        Keyed by the new name because that is what ``build_update_field_options``
        looks each option up by: mapping "Held" to the legacy option's id is
        precisely what turns a delete-and-create into a rename."""
        return {new: oid for new, (_, oid) in zip(self.new_names, self.options) if oid}


def plan_renames(field_cache_by_project: Mapping[str, Mapping]) -> list[Rename]:
    """Classify every board from its field cache. Pure: no network, no database."""
    plans: list[Rename] = []
    for slug in sorted(field_cache_by_project):
        status = ((field_cache_by_project[slug] or {}).get(STATUS_FIELD) or {})
        options: Mapping[str, str] = status.get("options") or {}
        field_id = status.get("field_id") or None
        ordered = tuple((name, options[name]) for name in options)
        index = (list(options).index(LEGACY_STATUS_NEEDS_HUMAN)
                 if LEGACY_STATUS_NEEDS_HUMAN in options else -1)
        if index < 0:
            plans.append(Rename(slug, DONE, field_id, ordered))
        elif STATUS_HELD in options:
            plans.append(Rename(
                slug, CONFLICT, field_id, ordered, index,
                reason="both columns exist; move the cards off the legacy one first"))
        elif not field_id:
            # ``_render`` refuses to write against a cache entry with no field id,
            # and so does this: the mutation has nothing to address.
            plans.append(Rename(slug, CONFLICT, field_id, ordered, index,
                                reason="Status field id missing from the cache; re-sync the board"))
        else:
            plans.append(Rename(slug, RENAME, field_id, ordered, index))
    return plans


def _data_or_raise(payload: dict, what: str) -> dict:
    errors = [str((e or {}).get("message") or e) for e in (payload.get("errors") or [])]
    if errors:
        raise RuntimeError(f"{what} failed: {errors[0]}")
    return payload


async def _save_to_db(board: Board, cache: dict) -> None:
    # Imported here, not at module scope: ``server.pm.mirror_store`` pulls in
    # ``server.automations``, which imports it straight back — a cycle Python
    # resolves only when something else (the app, a conftest) imported the chain
    # first. A script is that something else, so it must not be the entry point.
    from server.db import db_session
    from server.pm.mirror_store import save_field_cache

    async with db_session() as db:
        await db.run_sync(lambda s: save_field_cache(s, board.db_id, cache))


async def apply_rename(
    gh: GhRunner, rename: Rename, board: Board,
    *, save: Callable[[Board, dict], Awaitable[None]] = _save_to_db,
) -> dict[str, str]:
    """Perform one rename and refresh the board's cached fields.

    Returns the refreshed ``Status`` options (name -> id), which is also what the
    caller prints: the proof that the ids are the ones that went in."""
    if rename.status != RENAME or not rename.field_id:
        raise ValueError(f"{rename.project_slug}: not a renameable plan ({rename.status})")
    doc = build_update_field_options(rename.field_id, rename.new_names,
                                     option_ids=rename.option_ids)
    _data_or_raise(await gh(doc), f"rename on {rename.project_slug}")
    # Re-read the board rather than trusting the mutation's echo: the cache the
    # reconciler writes through must be what GitHub actually reports.
    fresh = parse_project_fields(
        _data_or_raise(await gh(build_project_fields_query(board.node_id)),
                       f"field re-read on {rename.project_slug}"))
    if not (fresh.get(STATUS_FIELD) or {}).get("field_id"):
        raise RuntimeError(f"field re-read on {rename.project_slug} returned no Status field")
    await save(board, fresh)
    return dict((fresh.get(STATUS_FIELD) or {}).get("options") or {})


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
    """Every mirrored board (optionally one), newest field cache included."""
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import GhProject, Project

    async with db_session() as db:
        rows = (await db.execute(
            select(Project.slug, GhProject.gh_node_id, GhProject.id, GhProject.field_cache)
            .select_from(GhProject)
            .join(Project, Project.id == GhProject.project_id)
        )).all()
    boards = [Board(str(r[0]), str(r[1] or ""), str(r[2]), dict(r[3] or {}))
              for r in rows if r[0]]
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
        # every board reads as "done" — a silent all-clear on the wrong database.
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
        return 0
    plans = plan_renames({slug: b.field_cache for slug, b in boards.items()})
    print_table(plans)
    conflicts = sum(1 for p in plans if p.status == CONFLICT)
    renameable = [p for p in plans if p.status == RENAME]

    if not args.apply:
        print(f"\n{len(renameable)} board(s) to rename, {conflicts} conflict(s) — "
              "re-run with --apply to perform them")
        return 1 if conflicts else 0

    token = os.environ.get(TOKEN_ENV) or ""
    if renameable and not token:
        # Fail BEFORE the first mutation: a half-authenticated run would leave the
        # fleet split across both spellings for no reason.
        print(f"error: {TOKEN_ENV} must be set to edit a board's fields", file=sys.stderr)
        return 2
    gh = gh_runner(token)
    failures = 0
    for plan in renameable:
        board = boards[plan.project_slug]
        try:
            options = await apply_rename(gh, plan, board)
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
