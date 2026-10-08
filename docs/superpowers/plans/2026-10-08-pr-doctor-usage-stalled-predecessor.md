# PR Doctor: a usage-stalled predecessor must not reserve product capacity — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Stop PR Doctor's priority gate from reserving a product's capacity for a PR whose fix is waiting on an attached session that is usage-stalled (paused because no account has headroom). Make that wait say what's wrong and how to clear it.

**Architecture:** `_pr_doctor_pending_products` (server/sessions/orchestrator.py) already moves targets PR Doctor provably can't act on into an `unclaimable` bucket (#2202, #2732). Extend it with one more evidence-based case: the target's newest `ownership_wait` event is the attached-live-session wait (`_pr_doctor_session_fix_owned`, no `claim_refused` marker, `session_id` set), AND that session is usage-stalled per the single source of truth `server/sessions/usage_stall.py:is_usage_stalled(status, paused_reason)`. Separately, the attached-session `ownership_wait` emit names a usage-stalled predecessor explicitly.

**Tech Stack:** Python 3 / pytest (`asyncio.run` style), existing orchestrator test fixtures.

**Spec (incident, 2026-10-08):** KinDash was held by `pr_doctor_priority` for #18420 ("capacity is reserved until it clears") from 09:37 to 13:08 ET. PR Doctor emitted 118 `ownership_wait` events: "cairnea/KinDash#18420 is still attached to session ed9d0b1f; stop its managed work before recovery". `ed9d0b1f` was a Codex session in `needs_attention` with `paused_reason="all_accounts_busy"`. The only Codex account was at its weekly limit until Oct 14, and a Codex session cannot rotate to Claude, so it would never stop or progress. Meanwhile the hold refused all new KinDash Backlog Runner work, which made it a deadlock. The existing #2202 guard (`_pr_doctor_claim_unclaimable`) only covers *refused* claims; this claim succeeded and then waited.

## Global Constraints

- Single source of truth: "usage-stalled" means `is_usage_stalled(record.status, record.paused_reason)`. Never re-derive it from `paused_reason` strings.
- D8: admission never kills a session. PR Doctor must NOT stop, release, or archive the predecessor. This change only stops *reserving capacity* for it and improves the message.
- Evidence, never inference (the #2202 docstring rule): un-gate only when the newest `ownership_wait` for the target names an attached session AND that session's current record is usage-stalled. A session that is merely idle, or actively working, keeps gating exactly as before.
- Python: `/home/tony/GitHub/viberpit-deploy/.venv/bin/python` (the main checkout's .venv is broken). Run tests with `env -u VIBERPIT_DAEMON_ROLE -u VIBERPIT_TAILNET_ALLOWLIST VIBERPIT_DEV=1` and every `VIBERPIT_SESSIOND_*` unset.
- Commits via `git commit -F <file>`; trailer `Co-Authored-By: Claude <your model> <noreply@anthropic.com>`. Never push.

## Review Focus

1. A predecessor that is attached and actively working (status `in_progress`) must keep gating. Pinned by a test.
2. A predecessor that is attached and `needs_attention` for a non-usage reason (e.g. `crash_cap`) keeps gating. Pinned by a test, so the change stays narrow.
3. If the stalled session later recovers (no longer usage-stalled), the gate re-applies on the next read with no sticky state. Pinned by a test that flips the record.
4. The read stays cheap on the admission hot path: reuse the single `list_events(kind="ownership_wait", limit=200)` read rather than adding a second one.

---

### Task 1: Un-gate a target waiting on a usage-stalled attached session; say so in the wait event

**Files:**
- Modify: `server/sessions/orchestrator.py` — `_pr_doctor_claim_refusals` (~16731) or a sibling reader over the same event list; `_pr_doctor_pending_products` (~16811); the attached-session `ownership_wait` emit in `_pr_doctor_session_fix_owned` (~15117).
- Test: the existing PR Doctor priority/unclaimable test file. Find it with `grep -rln "_pr_doctor_pending_products\|claim_unclaimable" server/tests` and follow its fixtures.

**Interfaces:**
- Produces: the newest-per-target read returns, for each target, either a claim-refusal head SHA (existing) or the attached session id from an attached-live-session wait (new). Keep ONE pass over `list_events(kind="ownership_wait", limit=200)`. Return a small dataclass or two dicts from one function rather than adding a second event read.
- Produces: the attached-session `ownership_wait` emit adds `detail={"attached_session": rec.id}`. When `is_usage_stalled(rec.status, rec.paused_reason)` holds, the summary reads: `"{target} is waiting on session {rec.id}, which is stalled on usage ({rec.paused_reason}); hand its work to a session that can run, or archive it, to let recovery start"`. Otherwise keep today's text.

- [ ] **Step 1: Failing tests** (in the existing PR Doctor priority test file, using its fixtures for a gated target):
  - target gated by a SessionFix whose newest `ownership_wait` names attached session S, with S's record `status="needs_attention", paused_reason="all_accounts_busy"` → `_pr_doctor_pending_products()` does NOT include the target's product (moved to unclaimable). The `pr_doctor_gate ... unclaimable=` INFO log names it.
  - the same with S `status="in_progress"` → still gating.
  - the same with S `status="needs_attention", paused_reason="crash_cap"` → still gating.
  - S usage-stalled → un-gated; then flip S to `status="in_progress", paused_reason=None` → gating again on the next call.
  - S's id not in `_records` (unknown or archived) → still gating. Evidence is required, and a session nobody knows is not proof of a stall.
  - the emit: driving `_pr_doctor_session_fix_owned` with a live, usage-stalled `rec` produces an `ownership_wait` event whose summary contains "stalled on usage" and whose detail has `attached_session`.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.** In the `pending` loop of `_pr_doctor_pending_products`, after the existing `own_op_stuck` / `_pr_doctor_claim_unclaimable` decision, a target whose newest wait names attached session S goes to the `unclaimable` bucket when `(rec := self._records.get(S)) is not None and is_usage_stalled(rec.status, rec.paused_reason)`. Update the method's docstring with one paragraph citing this incident (KinDash#18420, 2026-10-08). Update `_pr_doctor_claim_refusals`'s docstring if its return shape changes, and update every caller (grep).
- [ ] **Step 4: Run** the PR Doctor test files (`grep -rln "pr_doctor" server/tests | head -30`; run the ones touching priority, gate, unclaimable, refusals and session_fix), then `ruff check server/` and `mypy server/ --ignore-missing-imports`.
- [ ] **Step 5: Commit** `fix(pr-doctor): a usage-stalled attached session no longer reserves product capacity`.
