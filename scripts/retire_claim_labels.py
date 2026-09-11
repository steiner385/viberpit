#!/usr/bin/env python3
"""ONE-SHOT sweep that clears the retired Phase D claim labels (#994).

Phase D made the runner's durable rows the carrier of claim and give-up state:
``in-progress`` / ``needs-human`` are no longer read by anything. Open issues
labelled before the deploy still wear them, so this script runs ONCE, per product
repo, after the deploy — see ``specs/003-gh-projects-integration/phase-d-runbook.md``.

    python scripts/retire_claim_labels.py --repo steiner385/viberpit --repo cairnea/KinDash
    python scripts/retire_claim_labels.py --repo … --apply

Dry run by default: it prints the plan and touches nothing (and needs no database).
``--apply`` performs it, printing ``DONE``/``FAIL``/``SKIP`` per action and exiting
non-zero if anything failed. Re-running after a successful apply plans nothing —
the labels are gone — so a partial run is resumed by simply running it again.

What each action does (classification lives in ``server/pm/label_retirement.py``):

* ``row_from_bot_label`` — the runner placed the ``needs-human`` label, so it
  becomes the runner's ``gave_up`` row dated by the label event; the reconciler
  turns that into board Status ``Needs human`` and the queue into a cooldown.
* ``status_from_human_label`` — a human placed it, so it becomes a human Status
  edit to ``Needs human`` on the product's board, which the reconciler never
  overwrites. An issue that is on no board is SKIPPED with its label left in
  place: never strip a human's signal without replacing it.
* ``strip_in_progress`` — remove the label, nothing else.

This is the ONE sanctioned GitHub writer outside the reconciler (plan Task 4), and
it writes through the reconciler's own mutation builder so the GraphQL shape cannot
drift from it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from server.pm.label_retirement import (  # noqa: E402 -- repo root added above
    IN_PROGRESS, NEEDS_HUMAN, Action, plan_actions)

MIGRATION_REASON = "migrated from needs-human label"
TOKEN_ENV = "CCSWITCH_GH_PROJECT_TOKEN"


# --------------------------------------------------------------------------- #
# gh shell                                                                     #
# --------------------------------------------------------------------------- #

def _gh_env(token: str | None = None) -> dict[str, str]:
    """The child's env: ambient GitHub tokens stripped (ccswitch shells ``gh`` as
    its own identity), unless a token is passed for the Projects write."""
    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN")}
    if token:
        env["GITHUB_TOKEN"] = token
    return env


def _gh(args: list[str], *, token: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True,
                          env=_gh_env(token))


def _gh_json(args: list[str]) -> list:
    """Run a ``gh`` read and parse its stdout as one JSON array.

    ``--paginate`` can emit one array per page back to back, so decode
    repeatedly rather than assuming a single document.
    """
    proc = _gh(args)
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {(proc.stderr or '').strip()[:300]}")
    out: list = []
    decoder = json.JSONDecoder()
    text = (proc.stdout or "").strip()
    idx = 0
    while idx < len(text):
        value, end = decoder.raw_decode(text, idx)
        out.extend(value if isinstance(value, list) else [value])
        idx = end
        while idx < len(text) and text[idx] in " \r\n\t":
            idx += 1
    return out


def collect_repo(repo: str) -> list[Action]:
    """Plan one repo: list its open labelled issues, fetch the timeline of each
    ``needs-human`` one (only those need attribution) and plan over both."""
    issues: dict[int, dict] = {}
    for label in (IN_PROGRESS, NEEDS_HUMAN):
        for issue in _gh_json(["issue", "list", "--repo", repo, "--label", label,
                               "--state", "open", "--json", "number,labels",
                               "--limit", "500"]):
            issues[int(issue["number"])] = issue
    timelines: dict[int, list] = {}
    for number, issue in issues.items():
        if NEEDS_HUMAN in {(lb or {}).get("name") for lb in issue.get("labels") or ()}:
            timelines[number] = _gh_json(
                ["api", f"repos/{repo}/issues/{number}/timeline", "--paginate"])
    ordered = [issues[n] for n in sorted(issues)]
    return plan_actions(repo, ordered, timelines)


def _split_ref(ref: str) -> tuple[str, int]:
    repo, _, number = ref.rpartition("#")
    return repo, int(number)


def remove_label(ref: str, label: str) -> None:
    repo, number = _split_ref(ref)
    proc = _gh(["issue", "edit", str(number), "--repo", repo, "--remove-label", label])
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "").strip()[:300] or "gh issue edit failed")


# --------------------------------------------------------------------------- #
# apply                                                                        #
# --------------------------------------------------------------------------- #

async def write_gave_up_row(action: Action) -> None:
    """Replace the runner's ``needs-human`` label with the row Phase D reads.

    Shape and writer are the runner's own (``_give_up``): the same
    ``AutomationContext`` target state, so the queue's cooldown and the
    reconciler's Status derivation see a migrated handoff exactly as they see a
    live one.

    A row already exists for this target when the label outlived the deploy and
    the runner has since acted on the issue. ``set_target_state`` REPLACES, so
    writing blindly would clobber live state with a stale label's date:

    * a live claim — the issue is being worked right now; raise, and the caller
      SKIPs it with the label intact for a human to look at;
    * a real give-up — the row IS already the signal, dated by the decision that
      made it; leave it untouched and let the caller just drop the label."""
    from server.automations.context import AutomationContext
    from server.pm.queue_store import RUNNER_AUTOMATION_ID

    repo, _ = _split_ref(action.ref)
    ctx = AutomationContext(RUNNER_AUTOMATION_ID)
    existing = await ctx.get_target_state(action.ref)
    if existing.get("claimed"):
        raise LookupError("the runner holds a live claim on this issue")
    if existing.get("gave_up") and existing.get("reason") != MIGRATION_REASON:
        return  # already carried by a real give-up row; only the label is stale
    # An undated label event would make an unreadable ``needs_human_at`` — which
    # the queue reads as NO cooldown and the sweep deletes — so fall back to now:
    # a migrated handoff must park the issue, not quietly re-enter the pool.
    at = action.at or datetime.now(timezone.utc).isoformat()
    await ctx.set_target_state(action.ref, {
        "repo": repo,
        "issues": [action.ref],
        "gave_up": True,
        "needs_human_at": at,
        "reason": MIGRATION_REASON,
    })


def _board_item(db, repo: str, number: int) -> tuple[str, str, dict] | None:
    """``(item_node_id, project_node_id, field_cache)`` for this issue's board item."""
    from sqlalchemy import select

    from server.db.models import GhProject, GhProjectItem, WorkItem

    row = db.execute(
        select(GhProjectItem.gh_item_node_id, GhProject.gh_node_id, GhProject.field_cache)
        .join(WorkItem, WorkItem.id == GhProjectItem.work_item_id)
        .join(GhProject, GhProject.id == GhProjectItem.gh_project_id)
        .where(WorkItem.repo == repo, WorkItem.number == number, WorkItem.kind == "issue")
    ).first()
    if row is None or not row[0] or not row[1]:
        return None
    return str(row[0]), str(row[1]), dict(row[2] or {})


async def set_board_needs_human(action: Action, token: str) -> None:
    """Record a human's ``needs-human`` label as a human Status edit on the board.

    The mutation is built by the reconciler's own ``build_batch`` and read back by
    its ``parse_batch``, so this one-shot cannot drift from the shape the
    reconciler writes every tick (constitution VI).

    Raises ``LookupError`` when the issue is on no board — the caller SKIPs it and
    leaves the label alone."""
    from server.db import db_session
    from server.pm.gh_mutations import build_batch, parse_batch, payload_from_gh
    from server.pm.reconcile_diff import Mutation

    repo, number = _split_ref(action.ref)
    async with db_session() as db:
        found = await db.run_sync(lambda s: _board_item(s, repo, number))
    if found is None:
        raise LookupError("issue is on no board")
    item_node_id, project_node_id, field_cache = found

    doc, alias_map = build_batch(
        [Mutation("set_field", item_node_id, "Status", None, "Needs human", "migrate")],
        project_node_id=project_node_id, field_cache=field_cache)
    proc = await asyncio.to_thread(_gh, ["api", "graphql", "-f", f"query={doc}"],
                                   token=token)
    result = parse_batch(payload_from_gh(proc, partial_ok=True), alias_map)
    errors = [msg for msg in result.errors.values() if msg]
    if errors:
        raise RuntimeError(errors[0])


async def apply_action(action: Action, token: str) -> str:
    """Perform one action; returns the status word printed for it."""
    if action.kind == "strip_in_progress":
        await asyncio.to_thread(remove_label, action.ref, IN_PROGRESS)
        return "DONE"
    try:
        if action.kind == "row_from_bot_label":
            await write_gave_up_row(action)
        else:
            await set_board_needs_human(action, token)
    except LookupError as exc:
        # No replacement written ⇒ the label STAYS. A signal with nowhere to go is
        # left exactly as whoever raised it left it.
        return f"SKIP  ({exc})"
    # Label last, always: the durable replacement exists before the old signal
    # goes away, so a crash here is a re-runnable duplicate, never a lost handoff.
    await asyncio.to_thread(remove_label, action.ref, NEEDS_HUMAN)
    return "DONE"


# --------------------------------------------------------------------------- #
# entry point                                                                  #
# --------------------------------------------------------------------------- #

def _line(prefix: str, action: Action) -> str:
    return f"{prefix}  {action.kind}  {action.ref}  at={action.at} actor={action.actor}"


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", action="append", required=True, metavar="OWNER/REPO",
                    help="product repo to sweep; repeat for several")
    ap.add_argument("--apply", action="store_true",
                    help="perform the plan (without this, only print it)")
    ap.add_argument("--only", choices=[IN_PROGRESS, NEEDS_HUMAN],
                    help="restrict the sweep to one label")
    args = ap.parse_args()

    actions: list[Action] = []
    for repo in args.repo:
        actions.extend(collect_repo(repo))
    if args.only == IN_PROGRESS:
        actions = [a for a in actions if a.kind == "strip_in_progress"]
    elif args.only == NEEDS_HUMAN:
        actions = [a for a in actions if a.kind != "strip_in_progress"]

    if not args.apply:
        for action in actions:
            print(_line("DRY", action))
        print(f"\n{len(actions)} action(s) planned — re-run with --apply to perform them")
        return 0

    # Fail BEFORE the first mutation, not halfway through it: a missing token or
    # database would leave labels stripped with no replacement written. Each
    # input is demanded only when the plan actually needs it — an `--only
    # in-progress` sweep just removes labels and needs neither.
    token = os.environ.get(TOKEN_ENV) or ""
    if not token and any(a.kind == "status_from_human_label" for a in actions):
        print(f"error: {TOKEN_ENV} must be set to edit board Status", file=sys.stderr)
        return 2
    needs_db = any(a.kind != "strip_in_progress" for a in actions)
    if needs_db:
        if not os.environ.get("CCSWITCH_DB_URL"):
            # Without it the settings default would create an empty local SQLite
            # file and the rows would land there instead of in the live database.
            print("error: CCSWITCH_DB_URL must be set (the live database) to apply",
                  file=sys.stderr)
            return 2
        from server.db import init_db
        await init_db()

    failures = 0
    for action in actions:
        try:
            status = await apply_action(action, token)
        except Exception as exc:  # one failure must not abandon the rest of the sweep
            failures += 1
            status = f"FAIL  ({exc})"
        print(_line(status, action))
    print(f"\n{len(actions)} action(s), {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
