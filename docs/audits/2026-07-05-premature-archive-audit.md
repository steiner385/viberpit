# Retro audit — automated archives vs. the "armed-PR" completion bar

**Date:** 2026-07-05 · **Scope:** every `status='archived'` session archived by an
*automation* (`archived_by ∈ {autopilot, backlog-runner}`). Manual (`user`) and
legacy `NULL`-provenance archives were excluded by decision (a human `DELETE` is
explicit intent to abandon; a missing PR there is usually deliberate).

**Standard applied:** a session may only be archived once it has produced a *durably
landed* PR — merged, **or** currently open with auto-merge armed. Anything else on a
*code* goal is a premature archive. ("Research/Q&A" goals are exempt — no PR expected.)

## Method (routed through the shipped predicate, not a parallel copy)

The judgment reuses `server/automations/completion_bar.met_bar` — the *same* predicate
the live autopilot gate uses — via the existing `server/automations/archive_audit.py`
machinery. The retro run extends the shipped audit in four ways that the shipped tool
does **not** do (see [Logic gaps](#improvements-to-auto-archival-logic)):

1. **Automated-provenance scope** — only `autopilot` / `backlog-runner` rows.
2. **Intent from the transcript, not just the DB.** The initial prompt is *not*
   persisted (it lives in an in-memory `_pending_initial_prompt` map cleared on
   archive). Intent was reconstructed from each session's transcript `.jsonl`
   (`~/.claude-accounts/cfg/<account>/projects/*/<claude_session_id>.jsonl`), reconciled
   with the DB proxies (`active_goal` → `inferred_goal` → `title`). **Coverage: 48/48.**
3. **Referenced-PR resolution.** A large class of automated goals are *"land PR #N"*
   meta-tasks (consolidations, CI fixes) whose deliverable is an *existing* PR, not a
   new branch. We resolve `#N` from the goal **and** the session's own PR from the
   prompt's "your pull request #N" phrase (deliberately ignoring "PRs to combine (…)"
   input lists), then look each up live via `gh pr list`.
4. **Live-fate authority.** Only a *currently* merged/armed PR counts as landed. A PR
   that was merely *armed at archive time* (`archive_context.pr_state`) but has since
   **closed unmerged** is treated as a *silent failure*, not success — because
   auto-merge is not a guarantee (conflicts, supersession).

Two repos are involved; all PRs were bulk-fetched once each (`gh pr list --state all`),
then branch- and number-matched offline. Driver: `/tmp/run_archive_audit.py`
(read-only — it never unarchives).

## Results

| Provenance | In scope | Legit | Premature (candidates) |
|---|---|---|---|
| autopilot | 37 | 29 | **8** (7 abandoned-no-PR + 1 open-unarmed) |
| backlog-runner | 11 | 11 | 0 |
| **Total** | **48** | **40** | **8** |

> **Shipped `archive_audit.py` on the same 48 rows:** 25 legit / **21 ambiguous** / 2
> premature. It cannot resolve referenced PRs, so 21 *"land PR #N"* sessions collapse
> into "ambiguous" (report-only, never actioned) — it both under-credits landed work
> and cannot separate a real abandonment from a moot goal. The referenced-PR-aware run
> above resolves 20 of those 21 to a definite verdict.

All 8 candidates are **`cairnea/KinDash` autopilot sessions inside one "consolidate PRs
to save CI" campaign** (2026-07-04 → 07-05). That campaign churned: of 21 consolidation
PRs, 12 merged, 6 closed-unmerged, 3 still open. Each flagged session targeted a PR that
ended in the *closed* or *open-unmerged* set.

## Sessions to consider unarchiving

Ranked by actionability. IDs are the session UUID prefix.

### Tier 1 — actionable now (open PR, just never armed)

| Session | Created | Goal (abridged) | Why | Recommendation |
|---|---|---|---|---|
| `f505f347` | 07-05 20:25 | Merge consolidated PR **#11344** | #11344 is **OPEN + unarmed** today; archived as "goal met" without arming it | Verify #11344 isn't superseded by #11345/#11280, then **arm it** (`gh pr ready 11344 && gh pr merge 11344 --squash --auto`) or unarchive to finish. Low-cost, high-signal. |

### Tier 2 — target PR closed-unmerged; unarchive only if the underlying issues are still open

Each was recorded "goal met" on a PR that has since **closed without merging**. Because
12 sibling consolidation PRs *did* merge, the underlying commits may have landed via a
different consolidation — so these need a per-cluster human check (are the constituent
issues still open?) before unarchiving. Unarchiving a session whose work landed
elsewhere is wasted effort.

| Session | Created | Goal (abridged) | Target PR (now) |
|---|---|---|---|
| `42d894f1` | 07-04 19:32 | Consolidate 5 KinDash PRs, armed to merge | #11263 **closed** |
| `22c464bd` | 07-04 20:09 | Resolve merge conflicts in PR #11263, arm it | #11263 **closed** |
| `74ac8146` | 07-04 22:02 | Consolidate 10 PRs into #11266, armed | #11266 **closed** |
| `46dd5de6` | 07-04 22:49 | Consolidate 5 chore PRs, armed | #11267 **closed** |
| `6dd5a535` | 07-04 22:50 | Open consolidated COPPA PR (199 tests) | #11269 **closed** |
| `8e12057f` | 07-04 23:28 | Fix consolidation CI in PR #11267, armed | #11267 **closed** |
| `cc2c26ca` | 07-04 23:52 | Consolidate 5 PRs into #11273, arm auto-merge | #11273 **closed** |

> Archived sessions open via the **review route** (`/review/:id`), not `/session/:id` —
> they're out of the orchestrator's live record map.

**Not flagged (sanity checks that passed):** `1506c29a` ("Fix PLUS billing CI") — its
PR **#11288 merged** (the number was only in the initial prompt, caught by transcript
reconstruction). `9d2a509f` ("Fix PR Doctor spawning") — its PR **#471 merged** later.
`1577caf5` — PR **#469 merged**. All correctly legit.

## Improvements to auto-archival logic

Ordered by risk. File references are `server/…`.

1. **Backlog-runner has *no* completion gate at all.**
   `orchestrator._maybe_autoarchive_completed` (`sessions/orchestrator.py:967`) archives
   on `status="complete"` with zero PR check — `met_bar` is wired only into autopilot
   (`automations/autopilot_policy.py:157`). All 11 backlog archives landed *by luck*
   (their batch PRs merged), and all 11 recorded an **empty** `archive_context.pr_state`.
   **Fix:** route the backlog-runner completion path through the same completion bar,
   and snapshot `pr_state` there (via `_autopilot_pr_facts`) so the decision is gated
   and auditable.

2. **"Armed" is treated as terminal success, but auto-merge is not a guarantee.**
   `completion_bar.met_bar` returns `met=True, confidence="high"` for an `armed_pr`, and
   the session is archived. Auto-merge can still fail — conflicts, base drift,
   supersession — and the PR closes unmerged. **8 sessions** archived this way; their
   PRs never merged, and nothing re-checked. **Fix:** treat an armed PR as *pending*,
   not done — hold the session in a short "landing watch" until the PR actually
   `MERGED`s or `CLOSED`s, and/or add a **post-archive re-audit** that unarchives any
   session whose armed PR later closed-unmerged. (This retro run *is* that re-audit; the
   gap is that it doesn't run automatically and the shipped version can't see referenced
   PRs — see #3.)

3. **`archive_audit.py` joins only the session's own branches.**
   `build_pr_facts` (`automations/archive_audit.py:34`) matches `authored_branches` /
   `branch` against PR head refs, so every *"land PR #N"* meta-goal (21/48 here) falls
   into `ambiguous` and is dropped. **Fix:** extract PR numbers referenced in the goal
   (and the session's own PR from the initial prompt) and fold them into `PrFacts`; and
   score a currently-armed-but-later-closed PR as *not met*. This is what turned 21
   "ambiguous" into definite verdicts above.

4. **"Code goal + repo + zero PR" is low-confidence, so it isn't blocked.**
   The deterministic backstop only fires on `confidence == "high"`
   (`autopilot_policy.py:159`), while `met_bar`'s no-PR-at-all branch returns `"low"`
   (`completion_bar.py:86`). A repo-attached code session that opened **no** PR can
   therefore still auto-archive on the LLM decider's word. **Fix:** make "repo + code
   goal + zero PR" a hard *propose/report* (never a silent auto-archive), or raise it to
   high-confidence-blocked.

5. **The initial prompt isn't persisted — intent judgment depends on transcript files.**
   This audit reconstructed intent from `.jsonl` transcripts (worked 48/48, but those
   files can be garbage-collected). The ref-extraction gap on `1506c29a` (own PR only in
   the prompt) would vanish if the spawn recorded the session's own PR number. **Fix:**
   persist the initial prompt (or the resolved goal + own-PR number) on the `sessions`
   row so completion judgment and retro audits don't rely on ephemeral transcripts.

6. **Meta-observation (KinDash workflow, not ccswitch core):** autopilot repeatedly
   spawned overlapping consolidation sessions that superseded one another (6 closed
   consolidation PRs). Worth a dedup/"one live consolidation at a time" guard on the
   consolidation trigger, independent of archival.

## Reproducing

```bash
# read-only; requires gh auth + the daemon's DB
python3 /tmp/run_archive_audit.py          # writes /tmp/aa_report.json
# or the shipped tool (own-branch only, report-only):
CCSWITCH_DB_URL=sqlite+aiosqlite:///$HOME/.claude-accounts/ccswitch.db \
  python -m server.automations.archive_audit --dry-run
```
