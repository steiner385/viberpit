# Desktop-parity D-17: move a session to another host — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** add "Move to host…" for an idle Claude session.
1. Close the session on the source host.
2. Carry its worktree, uncommitted work included, to the target host through a hidden git ref.
3. Resume the same conversation there with the same session id and path.
4. Remove the source worktree once the target is running.
5. Restore to the source if any step fails.

**Facts (binding):** `docs/superpowers/specs/2026-11-05-d17-move-session-verification.md`. Use its §D code map and E.1 design, and correct the stale points in §6. The parent design is `docs/superpowers/specs/2026-09-14-cross-host-session-transfer-design.md`, Phase 1 manual move only.

**Owner decisions (2026-11-05):**
- **D1 (a): uncommitted changes travel by a WIP commit to a hidden transfer ref pushed to GitHub, only with explicit consent.** Use the shipped `src/viberpit/worktree_transfer.export`. Untracked files travel too, and the ref is deleted after import.
- **D4 (a): the source worktree is kept until the target's driver has started.** Then take a local `snapshot_recovery_ref` and `finalize` (remove) it.

**Controller rulings (2026-11-05),** taken on the spike's recommended options because they are safe and reduce scope:
- **D2 (a): Codex is not movable.** The capability `cross_host_transfer` is false, and the menu says "Codex history stays on its host".
- **D3 (a): idle sessions only.** `waiting_external` is allowed after a confirm that lists the background tasks that will stop; they are re-armed on the target. A running turn shows "Wait for the turn to finish or Interrupt first".
- **D5 (a): refuse when there are live steering children or a live steering parent.** Forks are exempt.
- **D6 (a): manual per-session move only.** No "Drain host", no rebalancer, no dead-host adoption (#949). Extract the per-row import and the predicate so #949 can reuse them.

**Decision:** D19 ("move a session between fleet hosts").
**Spec row:** `docs/superpowers/specs/2026-11-01-ui-desktop-parity-design.md` §11, D-17.

## Global Constraints

- **Movable predicate.** One pure function, used by the API, the menu and any later policy. The full list is in E.1 item 1:
  - Claude engine (`AgentCapabilities.cross_host_transfer`).
  - Not an assistant session; `display_mode` is not native.
  - Idle: not mid-turn, not inactive, not queued. Queued sessions move by restamp only; ruling: that's out of scope, refuse them.
  - No live steering parent or child.
  - The cwd is a managed worktree under the current base, so the path is identical on both hosts. Legacy `.ccswitch` trees are refused.
  - The target is enabled and healthy, passes the engine and platform checks, has capacity, and is not the source host.
  - The kill switch `VIBERPIT_SESSION_MOVE_ENABLED` is on (default true).
  - It returns a reason code per refusal, so the UI can explain it.
- **Saga** (E.1 items 2–6), with every step idempotent and restorable:
  - **Claim:** a nullable `sessions.moving_to` column, set with a compare-and-set. While it is set, per-session RPCs that would build a driver return 409 `transfer_in_flight` (code `SESSION_MOVING`).
  - **Export on the source:** a `transfer_export` RPC, routed to the source explicitly.
    - Re-check idle inside `_swapping`.
    - Tear down the driver.
    - Run `worktree_transfer.export`, with WIP only if `carryDirty`. If the tree is dirty and consent is missing, refuse with `MOVE_DIRTY_NEEDS_CONSENT` and list the dirty paths, capped at 50.
    - Release the lease.
    - Update or pop the source's in-memory record (§C.3 gap 1).
    - Expire the latest rewind undo.
    - Return the manifest, including pending preambles.
  - **Restamp at the edge:** set `host`, `moved_from` and `moved_at` (`db_utc_now()`, all nullable), set `_host_cache[sid] = target`, and write an audit row `session.move` with identity, from, to, carryDirty and outcome.
  - **Import on the target:** a `transfer_import` RPC.
    - Load the row through `_record_from_row`.
    - Run `worktree_transfer.import_` **before** `ensure_driver`, since `_ensure_session_cwd` refuses a missing tree.
    - `_claim_session(adopt=True)`, then `ensure_driver(resume=engine_session_id)`.
    - Re-arm background tasks.
    - Queue a one-line preamble: `This session moved from <source> to <target>; the worktree and uncommitted changes came with it.`
    - Delete the transfer refs.
  - **Finalize on the source** (D4): a `transfer_finalize` RPC, run only after the target driver is started. It takes `snapshot_recovery_ref`, then `finalize` (removes the worktree).
  - **Restore on any failure after export:** restamp back, import on the source (reuse path), and finish with outcome `restored`.
  - **Clear** `moving_to` in every terminal state. A crash mid-saga must not wedge the session: on startup or a heartbeat, a `moving_to` older than N minutes (ruling: 10) is reconciled by checking where the worktree and lease actually are. Log WARNING, because that is unexpected.
- **Split-brain** (§C.3): both gaps must be closed and tested.
  - The primary's in-memory `rec.host` must not resume the session in a leftover source tree.
  - A worker's `ensure_driver` must not return None for a row it hasn't rehydrated.
  - The route cache heals itself when the stamp changes.
- **Security:**
  - `POST /api/sessions/{id}/move {host, carryDirty}` requires a person (`require_human` / `human_sender`). Assistant and token callers get 403.
  - Write an audit row.
  - Expected refusals log at INFO.
  - The transfer ref name and every git argument are argv-only, never passed through a shell.
- **Migration:** chain `0105` (or the next number) off the newest revision on origin/master. All columns nullable.
- **Gates:**
  - Server: ruff, mypy, focused pytest, parity tests, then the full `server/tests -n 4` in a clean env before the PR.
  - Core `tests/` with `PYTHONPATH=$PWD/src` (`worktree_transfer` lives in `src/`).
  - Web: typecheck, lint, full vitest, build, budget, e2e.
  - Saga tests run against two fake orchestrators: one test per failure point, asserting the stamp, lease, `moving_to`, the in-memory host on both sides, and the audit row.

## Review Focus

1. A failure at each saga step leaves the session usable on exactly one host, with nothing wedged (Task 1).
2. A dirty tree is never carried without `carryDirty`, and the ref is deleted after import (Task 1).
3. No twin resume: neither the primary's stale in-memory record nor the source tree can start a driver after the move (Task 1).
4. A Codex session, a mid-turn session or one with steering children is refused, and the UI explains why (Tasks 1 and 2).
5. The source worktree is removed only after the target driver has started, and a recovery snapshot ref exists (Task 1).

---

### Task 0: Verification gate (Principle III), no product code

Run the gaps that block the design, against real infrastructure, and write the results into the facts doc (§ "Gate results").
1. **GitHub accepts the hidden ref.** From a scratch clone of `steiner385/viberpit` on dobby, use the same token path the product uses (`git_env_for_remote`):
   - push a throwaway commit to `refs/viberpit/transfer/d17-gate-<rand>`;
   - fetch it from rocky (`ssh rocky`, in a scratch clone);
   - delete the ref and confirm it is gone.

   Never touch a real session's worktree or branch.
2. **Cross-host resume.**
   - Pick an **archived** session (read-only on dobby) with a small transcript, or create a throwaway session in a scratch repo.
   - On rocky, create a worktree at the same path in a scratch clone using `worktree_transfer.import_` from a test ref.
   - Run `claude --resume <id> -p "reply with OK" --max-turns 1`, with the rocky CLI and a config dir that sees the transcript.
   - Record the result and the time taken, then clean up.
   - This is one tiny model call; accept the cost. If rocky can't run it safely, record why and stop for a ruling.
3. **Resume speed** over sshfs with the real `projects/` dir.

Commit `docs: D-17 verification gate results`. If any gate fails, stop and report; don't proceed to Task 1.

### Task 1: Server — predicate, saga, RPCs, route, reconcile

**Files:**
- `engine.py`: capability.
- New `server/sessions/session_move.py`: predicate and edge saga.
- `orchestrator.py`: `transfer_export`, `transfer_import`, `transfer_finalize` and the per-row import.
- `fleet_orchestrator.py`: routing and cache heal.
- RPC, remote and facade.
- Migration, `models.py`.
- `api/sessions.py`: route.
- `session_actions`.

**Tests:** `server/tests/test_session_move.py` and `tests/test_worktree_transfer*.py` as needed.

- [ ] **Step 1:** failing tests covering every bullet above plus Review Focus 1–5.
- [ ] **Step 2–4:** focused tests, parity, ruff, mypy, core.
- [ ] **Step 5:** commit `feat(fleet): move an idle Claude session to another host (D-17)`.

### Task 2: Web — "Move to host…"

**Files:**
- `features/sessions/SessionActions.tsx`: an `onMoveHost` item.
- A move dialog, using the Runner select pattern from `DispatchBar`:
  - the predicate's reason when disabled;
  - the dirty-file list with a consent checkbox, `Carry uncommitted changes (they are pushed to a hidden ref on GitHub and deleted after the move)`;
  - the background-task confirm.
- A "Moving to <host>…" chip on the card and header while `moving_to` is set.
- Hooks and mocks.

**Tests:** unit tests, and e2e `web/e2e/move-session.spec.ts` (route-mocked, 1440 and 390).

- [ ] **Step 1:** failing tests for the menu gating, dialog flows, consent, the chip, and Review Focus 4.
- [ ] **Step 2–4:** full web gates and e2e.
- [ ] **Step 5:** commit `feat(web): move a session to another host (D-17)`.

### Task 3: Benchmark and docs

- Benchmark: `Move session to cloud / other host` goes to P, with the note "other fleet host (D-17); no cloud". Regenerate §3 and report the target (D-16c was 95%).
- Spec §8/§10/§11: the design as built, D1–D6, and the follow-ups (Drain host, #949, Codex-as-new-thread).
- Product docs: the fleet doc.
- Ceilings: measured + 0.3, only if they moved.
- [ ] Commit `docs: D-17 benchmark rerun and move-session docs`.
