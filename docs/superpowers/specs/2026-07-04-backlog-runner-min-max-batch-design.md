# Backlog Runner — min/max issue-batch size

**Date:** 2026-07-04
**Status:** Design — pending review
**Feature area:** `server/automations/backlog_runner.py`, `server/automations/backlog_selection.py`, `server/automations/base.py`, `server/api/automations.py`

## Problem

The Backlog Runner bundles GitHub issues into a single PR per spawned session. Today the
operator can cap that bundle size (`max_issues_per_pr`), but cannot set a **floor**. We want:

- **Minimum** — every batch must contain *at least* this many issues, so we don't spawn a
  full implementation session for a trivially small bundle when more related work is available.
- **Maximum** — every batch must contain *at most* this many issues (already exists).

The minimum is best-effort by supply: when the backlog can't provide enough eligible issues
(**depletion**), we spawn with whatever is available rather than idling.

## Current behavior (verified against code, 2026-07-04)

Establishing the baseline precisely, because the feature builds directly on it.

- **Cap only truncates; it never fills.** `rank_and_batch` (`backlog_selection.py:56`) does
  `chosen = ordered[: max(1, max_issues_per_pr)]`, where `ordered` is the LLM ranking (filtered
  to known refs) or the lowest-number fallback. `test_partial_malformed_ranking_keeps_known_refs`
  (`test_backlog_rank_and_batch.py:56`) proves there is **no top-up**: cap=3 with a 2-ref ranking
  yields a batch of 2 even though a valid 3rd candidate exists.
- **No spawn-gating on size.** The loop (`backlog_runner.py:451-471`, `_refill`) spawns whatever
  `select_batch` returns; the only short-circuit is `batch is None`, which happens *only* when
  there are zero candidates (`backlog_selection.py:46`). A batch of 1 spawns as readily as 3.
- **The LLM is asked for a max-target, not a range.** `rank_issues` (`backlog_runner.py:83`)
  prompts for "the `{max_k}` most coherent issues... then the remaining in priority order." No
  minimum is communicated to the model.
- **Batches are not repo-constrained today.** Candidates can span repos and `rank_and_batch`
  does not restrict the chosen set to the primary's repo (see the multi-repo tests in
  `test_backlog_select_batch.py:137`). With the default cap=1 every batch is trivially single-repo,
  so this is a latent gap only at cap>1.

**Summary:** the runner asks for *up to* `max_k` coherent issues, truncates at the cap, and
spawns whatever comes back (≥1). It neither fills to a floor, asks for a range, nor gates spawning.

## Decisions (confirmed with the operator)

1. **Minimum semantics: hard floor with top-up.** Always put at least `min` issues in the batch.
   If the coherent group is smaller, top up with the next highest-ranked candidates even if not
   tightly related. Coherence is best-effort; the floor is guaranteed subject to availability.
2. **Depletion: spawn with whatever's available.** If fewer than `min` candidates exist (in the
   primary's repo), take all of them (down to 1) and spawn anyway. No idling, no grace timer.
3. **Invalid config (`min > max`): reject on save.** Validate in the config layer and refuse to
   persist, returning HTTP 422. Also reject `min < 1` and `max < 1`.

## Design

### 1. Config schema — new field

Add to `BacklogRunner.config_schema` (`backlog_runner.py`), placed before `max_issues_per_pr`:

```python
ConfigField(key="min_issues_per_pr", label="Min issues per PR", type="int", default=1,
            help="Always bundle at least this many issues into one PR. "
                 "Falls back to fewer only when the backlog can't supply enough."),
```

Default `1` reproduces today's behavior exactly (floor always already satisfied → no fill).

### 2. Pure batch logic — floor + top-up + single-repo guarantee

Extend `rank_and_batch` (`backlog_selection.py`) with a `min_issues_per_pr` parameter:

```python
def rank_and_batch(candidates, max_issues_per_pr, ranking, min_issues_per_pr=1) -> Batch | None:
    ...
    cap   = max(1, max_issues_per_pr)
    floor = min(max(1, min_issues_per_pr), cap)   # defensive clamp; save-time validation prevents min>max
    # determine `ordered` (ranking→known refs, else lowest-number fallback) as today
    chosen  = ordered[:cap]
    primary = chosen[0]
    repo    = by_ref[primary]["repo"]
    # SINGLE-REPO GUARANTEE: a PR targets one repo — keep only the primary's repo
    chosen = [r for r in chosen if by_ref[r]["repo"] == repo]
    # HARD FLOOR: top up with the NEXT-RANKED same-repo candidates. Priority source
    # is `ordered` itself (the LLM prompt returns the coherent group "then the
    # remaining issues in priority order"), falling back to any candidates missing
    # from `ordered` in lowest-number order. Stop at `floor` or when the repo is
    # exhausted (depletion).
    if len(chosen) < floor:
        picked = set(chosen)
        topup_order = ordered + [c["ref"] for c in sorted(candidates, key=lambda c: c["number"])]
        for ref in topup_order:
            if len(chosen) >= floor:
                break
            if by_ref[ref]["repo"] == repo and ref not in picked:
                chosen.append(ref); picked.add(ref)
    return Batch(primary=primary, issues=chosen[:cap], repo=repo)
```

Key properties:
- `floor <= cap` always (clamp), so top-up never exceeds the cap (`chosen[:cap]` is a belt-and-braces guard).
- Top-up prefers the LLM's own priority order (`ordered`), then any un-ranked leftovers by
  lowest number — faithful to "next-ranked" while staying deterministic when the ranking is empty.
- Top-up draws **only from the primary's repo** — closes the latent cross-repo gap and keeps the
  PR single-repo.
- **Depletion is automatic:** if the repo has fewer than `floor` candidates, the loop exhausts
  them and `chosen` ends up below `floor` — we return that smaller batch (spawn with whatever's
  available).
- With `min=1` (default): `floor=1`, `ordered` always yields ≥1, no top-up runs → **exact
  current behavior**, including `test_partial_malformed_ranking_keeps_known_refs`.

### 3. Wire the floor through selection

In `select_batch` (`backlog_runner.py:211`) read `min_issues_per_pr` from config and pass it to
`rank_and_batch`. The LLM `rank_issues` prompt is unchanged (still max-target); the floor is
enforced deterministically in the pure function, not by the model.

### 4. Reject `min > max` on save

The generic `validate_config` (`base.py`) stays single-field (type/enum only). Add a small
optional cross-field hook rather than speculative schema machinery:

- Pure rule in `backlog_selection.py`:
  ```python
  def validate_batch_bounds(config: dict) -> None:
      lo = int(config.get("min_issues_per_pr", 1))
      hi = int(config.get("max_issues_per_pr", 1))
      if lo < 1:  raise ValueError("min_issues_per_pr: must be >= 1")
      if hi < 1:  raise ValueError("max_issues_per_pr: must be >= 1")
      if lo > hi: raise ValueError("min_issues_per_pr cannot exceed max_issues_per_pr")
  ```
- `BacklogRunner.validate_config_extra(self, config)` calls it.
- `patch_automation` (`server/api/automations.py:126`) calls
  `getattr(automation, "validate_config_extra", None)` right after `validate_config`, inside the
  existing `try/except ValueError → HTTPException(422)`. Duck-typed via `getattr` so the other
  three automations need no change.

No frontend changes: the existing global `MutationCache.onError` toast already surfaces the 422
`detail` message.

## Isolation / boundaries

- `rank_and_batch` and `validate_batch_bounds` stay **pure** (no I/O) — heavily unit-testable,
  the dominant test surface.
- The API hook is a thin, duck-typed seam; only `BacklogRunner` opts in.
- No change to the LLM ranking seam, the loop's spawn/claim/sweep logic, or the DB schema
  (`min_issues_per_pr` lives in the existing `automation.config` JSON blob, default-filled by
  `validate_config`).

## Testing

Pure-function tests (mirroring `test_backlog_rank_and_batch.py` / `test_backlog_select_batch.py`):

- floor met by top-up when the coherent group is smaller than `min`;
- top-up stays within the primary's repo (multi-repo candidate pool);
- depletion: repo has fewer than `min` → batch is what's available, still spawns;
- `min=1` default leaves all existing outcomes unchanged (regression guard, incl. the
  partial-malformed case);
- `floor` clamped to `cap` when `min > max` reaches the pure function defensively.

`validate_batch_bounds` unit tests: `min<1`, `max<1`, `min>max` each raise; valid passes.

API test (mirroring `test_patch_bad_config_422`): `PATCH /api/automations/backlog-runner` with
`min_issues_per_pr > max_issues_per_pr` returns 422; a valid pair returns 200.

## Out of scope / YAGNI

- Generic per-field numeric bounds on `ConfigField` (only backlog needs cross-field rules today).
- Communicating the minimum to the LLM (the deterministic top-up covers it; the model still
  optimizes for coherence up to the cap).
- Grace-timer "wait for min then spawn stale" behavior (operator chose spawn-with-whatever).
