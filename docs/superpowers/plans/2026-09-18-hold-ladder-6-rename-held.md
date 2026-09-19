# Hold Ladder — 6: the rename to "Held" Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The board's machine value "Needs human" becomes "Held" everywhere the code writes or names it, boards are renamed in place by a one-time script without losing any card's assignment, the reconciler tolerates a mixed fleet during rollout, and the dead label-retirement code is gone.

**Architecture:** `STATUS_HELD = "Held"` replaces `STATUS_NEEDS_HUMAN` as the produced value; the legacy spelling survives ONLY as a read alias in one place (`desired_state.HELD_STATUS_SPELLINGS`), consulted by the three read paths (diff comparison, tier derivation, SDLC count) and by the one write path (`gh_mutations._render`, which picks whichever spelling the board's field cache has). The field bootstrap treats the pair as one option so it never adds a second. A dry-run-first script renames each board's option in place through the existing `build_update_field_options` primitive (ids preserved) and refreshes the field cache. The SDLC payload key becomes `held`; the web renders "Held". `label_retirement.py`, its script and test are deleted. The read alias is dropped in a follow-up PR once every board reports "Held".

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy · GitHub Projects v2 GraphQL via `gh` · React+TS · pytest, vitest.

**Spec:** `docs/superpowers/specs/2026-09-18-hold-ladder-design.md` §9 (the rename), §7 (board precedence writes `Held`), §14.

## Global Constraints

- Constitution VI: ONE definition of the spellings (`HELD_STATUS_SPELLINGS`), ONE normaliser (`desired_state.is_held_status(value) -> bool`) used by every read; ONE write-side resolver (`gh_mutations._render`). No module re-spells "Needs human" except the alias definition, the rename script's "from" name, and historical docs.
- The pre-ladder `triage_state == 'needs_human'`, `hold_reason = 'pre-ladder needs_human'`, migration 0049, `scripts/convert_legacy_needs_human.py`, and the Backlog Runner's internal `needs_human`/`_needs_human` seam names are a DIFFERENT concept and are OUT OF SCOPE — do not touch them.
- "Needs you" (the roadmap tier) stays. The web never shows a bare "Held" for a single item — the chip/dialog names the hold kind (3b/4b/5 already do); a "Held" COUNT label on the initiative stat tiles is fine.
- Rollout tolerance: a board whose Status option is still "Needs human" must keep working: reads treat both spellings as Held; writes use the cached spelling; the bootstrap never adds "Held" beside "Needs human".
- No schema change. No absolute date literals in tests. No new `CCSWITCH_` literals (the script reads its token from the existing `CCSWITCH_GH_PROJECT_TOKEN` name via the settings alias, not a new literal — read how `scripts/retire_claim_labels.py` did it BEFORE deleting it, and prefer `Settings.model_fields[...].alias` per the ratchet rule). Commit trailer literally `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`; messages via file + `git commit -F`; keep deploy/release/rollout/promote and prod/production apart in any command text.
- Test invocation: `env -i HOME=/tmp/vp-home PATH="$PATH" CCSWITCH_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit/.venv/bin/python -m pytest …` (foreground, never the whole `server/tests/`). Web gates: `(cd web && npm run typecheck && npm run lint && npm run test && npm run build)`.
- Branch `hold-ladder-6` off origin/master 418e696c (item 5, PR #2007, is unmerged; the branches touch `holds.py`'s docstrings at most — rebase before the PR).

## File structure

| File | Responsibility |
|---|---|
| `server/pm/desired_state.py` | `STATUS_HELD`, `LEGACY_STATUS_NEEDS_HUMAN`, `HELD_STATUS_SPELLINGS`, `is_held_status`; `STATUS_OPTIONS` carries `STATUS_HELD`; `derive_status` writes `STATUS_HELD` |
| `server/pm/reconcile_diff.py` | `_same` treats any two held spellings as equal for the Status field |
| `server/pm/gh_mutations.py` | `_render` resolves `STATUS_HELD` to the cached spelling's option id |
| `server/automations/gh_reconcile.py` | `_ensure_fields` treats the pair as one option (never adds "Held" beside "Needs human") |
| `server/pm/tree.py`, `server/api/sdlc.py` | reads via `is_held_status`; payload key `held` |
| `server/automations/backlog_github.py` | the give-up comment says "Held" |
| `scripts/rename_board_held.py` (new) + `server/tests/test_rename_board_held.py` | one-time rename, dry-run first |
| web: `api/sdlc.ts`, `features/roadmap/InitiativePage.tsx`, `initiatives/InitiativeTrack.tsx` (+ tests) | `held` count, "Held" label |
| deleted: `scripts/retire_claim_labels.py`, `server/pm/label_retirement.py`, `server/tests/test_retire_claim_labels.py` | dead code |
| docs | living docs; a "Holds" section; spec §14 |

---

### Task 1: The constant, the read alias, and the tolerant reconciler

**Files:** Modify `server/pm/desired_state.py`, `server/pm/reconcile_diff.py`, `server/pm/gh_mutations.py`, `server/automations/gh_reconcile.py`, `server/pm/tree.py`, `server/api/sdlc.py`, `server/automations/backlog_github.py`, `server/pm/queue.py` + `reconcile_inputs.py` (comments only). Tests: `test_desired_state.py`, `test_reconcile_inputs.py`, `test_pm_tree.py`, `test_backlog_github.py` (the "Held not in text" assertion flips), `test_pm_api.py`, `test_pm_reconcile_api.py`, `test_pm_holds.py`, `test_pm_queue.py`, `test_gh_reconcile_loop.py`, `test_sdlc_*` (grep `needsHuman`), `test_gh_mutations*.py`, `test_reconcile_diff*.py`.

**Interfaces:**
- `desired_state.STATUS_HELD = "Held"`; `LEGACY_STATUS_NEEDS_HUMAN = "Needs human"` (read alias; comment: dropped in the follow-up PR once every board reports Held); `HELD_STATUS_SPELLINGS = frozenset({STATUS_HELD, LEGACY_STATUS_NEEDS_HUMAN})`; `is_held_status(value: str | None) -> bool`. `STATUS_NEEDS_HUMAN` is REMOVED (every importer moves to `STATUS_HELD` or `is_held_status`); `STATUS_OPTIONS` lists `STATUS_HELD` in the old slot.
- `derive_status` → `Decision(STATUS_HELD, …)` on both held branches.
- `reconcile_diff._same(a, b, *, field)`: for the Status field, `is_held_status(a) and is_held_status(b)` counts as equal (so a board still spelled the old way is not re-diffed every tick).
- `gh_mutations._render`: when `m.field == "Status"` and `m.new_value == STATUS_HELD`, try `options[STATUS_HELD]` then `options[LEGACY_STATUS_NEEDS_HUMAN]`; only then `UnknownOption`. A board whose cache has both prefers `STATUS_HELD`.
- `gh_reconcile._ensure_fields`: when computing missing Status options, treat `STATUS_HELD` as present if ANY spelling in `HELD_STATUS_SPELLINGS` exists on the board — never create a second option.
- `tree.issue_tier`: `is_held_status(board_status)`.
- `api/sdlc.py`: `"held": sum(... is_held_status(status))` — the key `needsHuman` is REMOVED (Task 3 moves the web in the same PR).
- `backlog_github.announce_needs_human` text: "Board Status is Held until a person releases it…".

- [ ] **Step 1 — failing tests**: `test_desired_state.py`: `derive_status` yields "Held" for a held item and for a human-wait session; `STATUS_OPTIONS` contains "Held" and not "Needs human"; `is_held_status` true for both spellings, false for "Ready"/None. `test_reconcile_diff*.py`: desired "Held" vs mirror "Needs human" → no Status mutation; vs "Ready" → a mutation. `test_gh_mutations*.py`: `_render` with a cache holding only "Needs human" → that option id; only "Held" → that id; both → "Held"'s; neither → `UnknownOption`. `test_gh_reconcile_loop.py`: a board with "Needs human" and no "Held" → `_ensure_fields` adds nothing for the pair; a board with neither → adds "Held". `test_pm_tree.py`: both spellings → needs-you. SDLC test: payload has `held`, not `needsHuman`. `test_backlog_github.py`: the comment says "Held" (flip the existing assertion). Parametrise reads over both spellings; writes assert the cached spelling.
- [ ] **Step 2** run → fail. **Step 3** implement. **Step 4** run the listed files + ruff + mypy on the touched modules + brand ratchet + `grep -rn '"Needs human"' server/ --include=*.py | grep -v tests/` (must print only the alias definition and the rename script if present). **Step 5** commit `feat(pm): the board value is "Held"; the reconciler reads both spellings and writes the cached one`.

---

### Task 2: The one-time board rename script

**Files:** Create `scripts/rename_board_held.py`, `server/tests/test_rename_board_held.py`. Read `scripts/retire_claim_labels.py` (before Task 4 deletes it) for the auth/DB conventions: explicit `--apply` (dry-run default), token from the `CCSWITCH_GH_PROJECT_TOKEN` setting (use the settings alias, no new literal), DB URL from settings with an explicit-env guard, `subprocess.run(["gh", ...])` with ambient `GITHUB_TOKEN`/`GH_TOKEN` stripped.

**Interfaces:**
- `plan_renames(field_cache_by_project: dict[str, dict]) -> list[Rename]` — pure: for each project whose Status options contain `LEGACY_STATUS_NEEDS_HUMAN` and NOT `STATUS_HELD`, one `Rename(project_slug, field_id, options_in_order_with_ids, renamed_index)`; a project with both spellings is reported as `conflict` (needs a human: cards must be moved before the old option is deleted) and skipped; a project already on "Held" is `done`.
- `apply_rename(gh, rename) -> None` — builds the mutation with `server/pm/gh_mutations.build_update_field_options(field_id, options, option_ids=…)` (ids preserved, one `name` changed), executes it, then re-fetches fields with the existing query helpers and `save_field_cache`.
- CLI: `python scripts/rename_board_held.py [--apply] [--project SLUG]`; prints a table `slug  status(done|rename|conflict)  option-id`; `--apply` performs the renames and prints the refreshed cache's Status options per board; exit 1 on any `conflict`.
- Test: pure `plan_renames` over three fake caches (legacy only / both / held only); `apply_rename` with a fake `gh` runner asserting the mutation carries every existing option id and the renamed name, and that the cache refresh is saved.

- [ ] Commit `feat(scripts): rename_board_held — rename the board option in place, dry-run first`.

---

### Task 3: The web rename

**Files:** `web/src/api/sdlc.ts` (`held: number`, drop `needsHuman`), `web/src/features/roadmap/InitiativePage.tsx` (stat tile label "Held"), `web/src/initiatives/InitiativeTrack.tsx` (aria text "held"), their three tests, `web/src/features/roadmap/detail/LeafList.test.tsx` and `web/src/api/__tests__/triageHooks.test.tsx` only if they mention the old value (grep). Run the FULL web gates. Commit `feat(roadmap): initiative counts say Held`.

---

### Task 4: Dead code removal

**Files:** delete `scripts/retire_claim_labels.py`, `server/pm/label_retirement.py`, `server/tests/test_retire_claim_labels.py`; keep `specs/003-gh-projects-integration/phase-d-runbook.md` (the record). Grep for any remaining importer (`label_retirement`, `retire_claim_labels`) in server/, scripts/, deploy/, docs/product — living docs that reference the script as a current tool get one sentence pointing at the runbook. Run `ruff`, `mypy server/`, the brand ratchet (deleting a file can only lower counts — run `python scripts/brand_ratchet.py --update` ONLY if it reports a count went DOWN, and commit the toml). Commit `chore(pm): remove the one-time claim-label retirement code (sweep ran 2026-09-11)`.

---

### Task 5: Docs, gates, PR

- Living docs: `docs/product/features/product-management-layer.md` — turn the bold "Holds" paragraph into a `## Holds` section that is the single description of the ladder (kinds, who sets what, the board value "Held", the release signals, the ladder, evidence dispatch, decision proposals — one paragraph each, linking the runner doc for detail), and fix line ~70's "rename … still later series items"; `docs/product/features/session-tools.md:~155`, `autopilot-backlog-runner.md:~483`, `docs/superpowers/specs/2026-09-07-roadmap-drilldown-design.md:~323`, `specs/003-gh-projects-integration/spec.md:~90,141`, `phase-b-runbook.md:~31,71`, `phase0-findings.md:~181`, `plan-phase-c.md:~304` — the board value is "Held". Historical plan/spec-log docs stay. Spec §14: one bullet — "read alias `LEGACY_STATUS_NEEDS_HUMAN` is dropped in a follow-up PR once `scripts/rename_board_held.py` reports every board `done`".
- Ops note in the runner doc (or the PM-layer doc's Holds section): after deploy, run `scripts/rename_board_held.py` (dry-run, then `--apply`) on the edge host.
- Gates: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `python scripts/brand_ratchet.py`; the controller runs `server/tests/` detached. Commit `docs: the board value is Held; one Holds section`.

## Self-review

**Spec §9 coverage:** board option renamed in place via `updateProjectV2Field` keeping assignments (T2, reusing `build_update_field_options` with ids); mixed-board tolerance — reads both, writes the cached spelling (T1: `_same`, `_render`, `_ensure_fields`, `is_held_status`); `STATUS_HELD` in `desired_state.py` with production and test modules following (T1); SDLC `needsHuman` → `held` with its web consumers in the same change (T1 + T3); the web renders "Held" never bare (T3; the chip/dialog already name the kind); the docs and the single "Holds" section (T5); dead code removed with the runbook kept (T4); ordering — last PR of the series; read-alias drop is a follow-up (§14 bullet).

**Type consistency:** `STATUS_HELD`, `LEGACY_STATUS_NEEDS_HUMAN`, `HELD_STATUS_SPELLINGS`, `is_held_status` (T1) used by T2's `plan_renames` and by T5's docs; `build_update_field_options(field_id, options, *, option_ids)` (existing) used by T2; the payload key `held` (T1) read by T3.
