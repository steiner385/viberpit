# Fix pattern: a relaxation fallback that cannot tell a caller's hard constraint from a system hint

**Date:** 2026-09-08
**Origin:** production — every live session parked as "no usage headroom" while
`tony_kindash.com` sat at 46% weekly. Fixed in `pick_account_detailed`
(`src/ccswitch/accounts.py`) and both rotation call sites
(`server/sessions/orchestrator.py`).

## The bug signature

A function takes a **filter set** and, when the filtered search comes up empty, retries with the
filter progressively removed so a transient condition can't starve the caller. The relaxation is
correct for the constraints the *function* owns — a soft reservation, a time-boxed quarantine, a
backoff. It is wrong for the constraints the *caller* supplied, because those encode first-hand
knowledge the function does not have and cannot re-derive.

When both kinds arrive in one parameter, the relaxation hands back exactly the thing the caller
ruled out, and does it *silently* — the call succeeds, so no error, no log line, no alert.

The instance: `_rotate_away` passed `exclude = quarantined | {current_account}` — a self-healing
hint unioned with "this account just rate-limited the live session I am moving off it". The
fallback's first tier dropped the whole set:

```python
for excl, rsv in [(frozenset(), reserved), (exclude, frozenset()), (frozenset(), frozenset())]:
```

## Why it stayed invisible for so long

Three properties compounded, and each is worth recognising on its own:

1. **The excluded thing looked healthy to the function.** Anthropic returns 429 before reported
   usage reaches the 100% ceiling, and the cached snapshot is up to 60s stale. So the
   just-rate-limited account was not `at_ceiling`, scored *best* on the perishability metric, and
   was the natural pick the moment the exclusion lifted. Only the caller knew it had just failed.
2. **The relaxation succeeded, so nothing was logged as wrong.** The log line read
   `reassign.complete session=… tony_steinonline.net→tony_steinonline.net reason=rate_limit` —
   a success message for a no-op.
3. **The symptom named the wrong cause.** Each no-op rotation consumed one of the 3-per-300s
   rotation budget; the fourth parked the session as `rotation_cap`, which `is_usage_stalled()`
   renders as "parked for lack of usage headroom". The dashboard blamed usage exhaustion for a
   selection bug, which is why the accounts page and the session cards disagreed.

Ordering made it worse: the excluded account was resurrected in tier 1, *before* tier 2 would have
tried relaxing a merely stale reservation. The fallback preferred the known-bad account over a
probably-fine one.

## Fix shape

Split the parameter by **who owns the constraint and whether it heals on its own**:

- `exclude` — caller-owned, hard. Present in every fallback tier. If honouring it leaves nothing,
  that is a real `AllAccountsBusy`, and the caller parks with an earliest-reset the user can read.
  A truthful "nothing to move to, retry at 21:00" beats a false success.
- `soft_exclude` — function-owned, relaxable. Quarantine, reservations, backoff.

```python
hard = set(exclude)
soft = set(soft_exclude) - hard
try:
    picked = _pick(hard | soft, reserved)
except AllAccountsBusy as _first_exc:
    if not soft and not reserved:
        raise  # nothing relaxable — the constraint is real, not a hint
    for excl, rsv in [(hard, reserved), (hard | soft, frozenset()), (hard, frozenset())]:
        ...
```

## Where to look for siblings

Swept 2026-09-08; the shape appears once in this repo, but these are the places it would recur:

- **Any `except <Empty>: retry with fewer filters` block.** Grep for a retry that passes
  `frozenset()` / `set()` / `None` where the first attempt passed a caller argument.
- **Every caller that passes an accumulating "already tried" set.** `src/ccswitch/runner.py`
  (`exclude=tried`) and `src/ccswitch/launcher.py` (`exclude={name}`) were both defeated by the same
  fallback: `tried` stopped growing, so the "all accounts tried" exit was unreachable and ccrun
  respawned on a failing account. Both are fixed by the central change — no call-site edit needed,
  which is the tell that the defect was always in the shared function.
- **Ranking tiers are NOT this shape and are safe.** `rank_accounts`'s `optimistic` /
  `reserve_blocked` / `extra_paid` tiers *demote* candidates rather than re-running the filter, and
  the `if name in exclude or name in reserved: continue` check runs before any tier is built, so an
  excluded name can never appear in one. Demote-don't-refilter is the pattern to prefer.

## Regression tests

- `tests/test_pick_account_detailed.py` — `exclude` survives every tier; `soft_exclude` is still
  relaxed last-resort; relaxing a reservation never resurrects an excluded account; a growing
  `exclude` set is honoured in full.
- `server/tests/test_rotate_never_returns_same_account.py` — `_rotate_away` and `rotate_to_best`
  pause with `all_accounts_busy` rather than reassigning A→A, and pass current-account hard /
  quarantine soft.
