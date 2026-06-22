# Load-aware account selection

**Date:** 2026-06-22
**Status:** Approved (design)
**Feature area:** 001-ccswitch-web · account selection / rotation

## Problem

Account selection (`select_best` in `src/ccswitch/selector.py`) ranks accounts purely
from per-account `Usage` data — perishability first, then lowest usage. It has no notion
of how many sessions are *already live* on an account. Because concurrent sessions on one
account share that account's rate limit, piling new (or rotated) sessions onto a single
account drives 429 / usage-limit errors even when other accounts are idle.

We want session-count to influence selection so load is spread across accounts, **without
discarding** the existing perishability-first behavior.

## Decisions (from brainstorming)

1. **Role of count:** a *weighted factor* folded into the score — not a hard cap and not a
   pure tiebreaker. A heavily-loaded account is penalized even when it ranks well on
   perishability.
2. **What counts as load:** *live sessions weighted by activity* — every session with a live
   driver contributes, but actively-working sessions weigh more than idle ones.
3. **Formula:** *multiplicative discount* — load shrinks an account's perishability score.
   Bounded, no NaN, and reduces exactly to today's behavior when load is zero. Fully-fresh
   accounts (perishability 0) remain saved for last.

## Architecture

The selector core (`src/ccswitch/`) is deliberately decoupled from the server. Live-session
counts live only in the server's `SessionOrchestrator._drivers`. We therefore pass load as
**plain data** down through the existing call chain, keeping `select_best` a pure function:

```
SessionOrchestrator.live_session_load()  ->  dict[str, float]      # server owns runtime state
        |
        v  passed as `load=` at both call sites (create_session, _rotate_away)
server/sessions/selection.py: select_account(exclude, load)
        |
        v
ccswitch/accounts.py: pick_account(cfg, exclude, load)
        |
        v
ccswitch/selector.py: select_best(usages, reserved, cfg, now, exclude, load)
```

Every layer defaults `load={}` (empty dict), so CLI usage and existing tests are unchanged.

## Components

### 1. Load signal — `SessionOrchestrator.live_session_load()`

New method on `server/sessions/orchestrator.py`:

```python
def live_session_load(self) -> dict[str, float]:
    """Weighted count of live sessions per account, for load-aware selection.

    Working sessions (_ACTIVE_STATUSES) weigh 1.0; idle sessions weigh
    cfg.idle_session_weight. Accounts with no live sessions are absent (load 0).
    """
    cfg = load_config()
    load: dict[str, float] = {}
    for sid in list(self._drivers.keys()):       # snapshot: _drivers mutates on the loop
        rec = self._records.get(sid)
        if rec is None:
            continue
        weight = 1.0 if rec.status in _ACTIVE_STATUSES else cfg.idle_session_weight
        load[rec.account] = load.get(rec.account, 0.0) + weight
    return load
```

- "Live" = has an entry in `_drivers` (same liveness notion as `has_live_session`). Detached /
  ended / archived records (no live driver) do not count.
- Status comes from the in-memory `SessionRecord` (`_records[sid].status`), reusing the
  existing `_ACTIVE_STATUSES = {"in_progress", "working", "waiting_input"}` set.
- `list(self._drivers.keys())` snapshots before iterating, matching the existing
  concurrency-safety pattern in `has_live_session` (the event loop mutates `_drivers`).
- `load_config()` per call mirrors `select_account`, which already reloads config each call.

### 2. Threading — `select_account` / `pick_account` / `select_best`

- `server/sessions/selection.py`:
  `def select_account(exclude=frozenset(), load: dict[str, float] | None = None) -> str:`
  forwards `load or {}` to `pick_account`.
- `src/ccswitch/accounts.py`:
  `def pick_account(cfg, exclude=frozenset(), load: dict[str, float] | None = None) -> str:`
  forwards `load or {}` to `select_best`. (Reservation/ledger logic unchanged.)
- `server/sessions/orchestrator.py`: both call sites pass the live map:
  - `create_session` (~L279): `select_account(exclude=…, load=self.live_session_load())`
  - `_rotate_away` (~L1137): `select_account(exclude=exclude, load=self.live_session_load())`

### 3. Scoring — `select_best`

```python
def select_best(usages, reserved, cfg, now, exclude=frozenset(),
                load: dict[str, float] | None = None) -> str:
    load = load or {}
    ceiling = cfg.hard_ceiling * 100.0
    candidates: list[tuple[float, float, float, float, str]] = []
    ...
    for name, u in usages.items():
        ...                                   # exclude / reserved / None / ceiling filters unchanged
        l = load.get(name, 0.0)
        perish = _perishability(u, ceiling, now) if cfg.prefer_soon_reset else 0.0
        composite = perish / (1.0 + cfg.load_sensitivity * l)
        candidates.append((-composite, l, u.session_pct, u.weekly_pct, name))
    ...
    candidates.sort()
    return candidates[0][4]
```

**Sort key:** `(-composite, l, session_pct, weekly_pct, name)`.

- `composite` (load-discounted perishability) is the primary key — this is where load is
  *weighted against* perishability.
- `l` (raw weighted load) is the **explicit secondary key**. Critical for the regime where
  perishability cannot differentiate: when `prefer_soon_reset=False` or every candidate is
  fresh, all `composite` values are 0 and a multiplicative discount of 0 stays 0 — load would
  otherwise have no effect. The explicit `l` key makes load still spread sessions there
  (fewest first), before falling back to the legacy usage→name ordering.

### 4. Config knobs — `src/ccswitch/config.py`

Added to the `Config` dataclass (JSON-overridable like the rest):

```python
load_sensitivity: float = 0.5     # λ: how hard live load discounts perishability
idle_session_weight: float = 0.3  # weight of an idle live session (working = 1.0)
```

## Behavior / edge cases

- **`load={}` (CLI, no live sessions):** `composite = perish / (1 + 0) = perish` and every
  `l = 0`, so the 5-tuple reduces to today's `(-perish, 0, session_pct, weekly_pct, name)` —
  identical ordering to the current 4-tuple. Fully backward compatible.
- **Bounded:** `1 + λ·l ≥ 1` always (λ, l ≥ 0), so no divide-by-zero and no NaN/∞.
- **Fresh accounts:** perishability 0 ⇒ composite 0 ⇒ remain last (the multiplicative choice
  preserves "save fresh capacity for last").
- **All-at-ceiling:** unchanged — still raises `AllAccountsBusy(earliest_reset)`.

## Testing

- `tests/test_selector.py`:
  - busy-but-perishable account demoted below a less-busy perishable peer (composite ordering);
  - `load={}` reproduces the legacy ordering exactly (regression guard);
  - with `prefer_soon_reset=False` (all composite 0), load breaks ties (fewest sessions first);
  - idle vs working weighting changes the ranking as expected;
  - bounded: large load never crashes / never NaN.
- `tests/test_accounts.py`: `pick_account` forwards `load` to `select_best`.
- `server/tests/`: `live_session_load()` weights working vs idle correctly, ignores
  detached/absent records, and snapshots `_drivers` safely; `create_session` and `_rotate_away`
  pass the live map into `select_account`.

## Non-goals

- No hard per-account session cap (explicitly declined in brainstorming).
- No persistence of load (it is derived live from `_drivers` each call).
- No change to the 429/401 quarantine / rotation-cap recovery logic — load-awareness composes
  with it (quarantined accounts are still excluded before scoring).
