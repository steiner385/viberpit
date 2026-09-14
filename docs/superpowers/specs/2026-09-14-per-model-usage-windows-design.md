# Per-model usage windows: report them, and switch accounts on a per-model limit

**Date:** 2026-09-14
**Status:** approved (design) — PR 2 of the usage-stalled rotation work
(`2026-09-04-usage-stalled-rotation-design.md` named this follow-up)
**Area:** `src/ccswitch/oauth.py`, `quota.py`, `selector.py`, `accounts.py`;
`server/sessions/` (selection, orchestrator, model_policy, translator, events);
`web/src/lib/usageWindows.ts` and its consumers.

## Problem

Anthropic meters some models separately from the account-wide windows. Verified live on
2026-09-14 against `GET /api/oauth/usage` for two accounts on this host:

| account | session (5h) | weekly (all) | weekly, scope `model.display_name="Fable"` |
|---|---|---|---|
| `tony_steinonline.net` | 2% | 80% | 62% |
| `tony_kindash.com` | 3% | 91% | **100%**, `is_active: true` |

The scoped entry arrives as a third element of `limits[]`:

```jsonc
{ "kind": "weekly_scoped", "group": "weekly", "percent": 100, "severity": "critical",
  "resets_at": "2026-09-14T19:59:59Z",
  "scope": { "model": { "id": null, "display_name": "Fable" }, "surface": null },
  "is_active": true }
```

Note `scope.model.id` is null — the model is identified by display name only. The
legacy top-level keys (`seven_day_opus`, `seven_day_sonnet`) are null on both accounts.

ViberPit drops this entry. `oauth.parse_usage` reads the first `session` and the first
`weekly` group entry and nothing else, so:

1. **Reporting.** The Accounts page, session cards and `ccswitch list` show
   `tony_kindash.com` at 91% weekly. It is in fact unusable for Fable and fine for Sonnet
   and Opus. Nobody can see that.
2. **Selection.** `select_account` hands a Fable session to that account; its first turn
   429s; failure recovery quarantines the account for 60 s and rotates. The rotation
   target is picked model-blind, so it can land on another Fable-exhausted account and
   burn a slot of the per-session rotation cap.
3. **Rotation.** The poller-driven `_rotation_monitor` is account-scoped: it either
   moves every session on an account or none. A Fable-only exhaustion moves nothing
   (the account-wide windows are under the ceiling), and an account-wide exhaustion
   cannot distinguish sessions that were already going to fail from ones that were not.

## What already exists (route through, do not duplicate)

- `QuotaWindow` already has `scope` (`SCOPE_ACCOUNT` / `SCOPE_MODEL`) and
  `applies_to_models`; `QuotaSnapshot.windows_for(model)`, `binding_window(model)`,
  `windows_at_or_over(pct, model)` are model-aware; the ledger blob, the RPC encoding,
  `GET /api/accounts` (`appliesToModels`) and the TS `UsageWindow` type all carry it.
  The Codex quota reader (`server/codex/quota.py:_window`) already produces model-scoped
  windows. Anthropic is the only producer that does not.
- `selector.rank_accounts`, `at_ceiling`, `exhausted`, `_perishability` all accept
  `model`. Nothing above them passes one: `accounts.pick_account_detailed`,
  `server/sessions/selection.select_account` and every orchestrator call site omit it.
- `QuotaWindow.applies_to(None)` returns True for model-scoped windows **on purpose**
  (documented in `quota.py`): an unknown model must not be handed an account that 429s
  on its first turn. This means the moment the parser emits the Fable window, every
  model-blind caller treats `tony_kindash.com` as exhausted for everything. That is the
  conservative direction, but it is why the model must be threaded through selection and
  rotation **in the same change** as the parser.
- Model tier vocabulary: `server/sessions/model_catalog.py` (`TIER_ORDER`, `model_tier`,
  `normalized_alias`) and `model_policy.launch_model` (resolves `default`/None to the
  fleet-wide inherited model, #1460). The web mirror is `session/modelName.ts:modelTier`.
- The SDK's `RateLimitEvent.rate_limit_info.rate_limit_type` is one of `five_hour`,
  `seven_day`, `seven_day_opus`, `seven_day_sonnet`, `overage` (claude-agent-sdk 0.2.105
  `types.py:1236`). `translator.py:260` keeps only `resets_at` and `status`.

## Goal

1. Every surface that shows account usage shows the per-model window(s) Anthropic
   reports, next to the account-wide ones.
2. A new session, a rotation target and a manual "next best account" pick are chosen for
   the model the session will run, so a Fable-exhausted account still serves Sonnet and
   is never handed to Fable.
3. The poller-driven rotation moves only the sessions whose model the exhausted window
   meters, and names that window in the reason and the notification.

## Non-goals

- Downgrading a session's model when every account is exhausted for it. That is a policy
  question (cost versus continuity) with its own approval semantics; this change parks
  such a session as `all_accounts_busy` naming the model, as today.
- Per-model reserves (`weeklyReservePct` stays bound to the account-wide weekly window).
- A `?model=` parameter on `GET /api/accounts` ranking. The rank column keeps its
  "unknown model" meaning; the per-model badges carry the information.
- Codex per-model selection. Non-Claude engines keep passing `model=None` (today's
  conservative behaviour). Only the Claude engine has a tier vocabulary to map through.
- Acting on `RateLimitInfo.status == "rejected"` pre-emptively. The type is carried for
  observability; recovery policy is unchanged.

## Design

### 1. Producer — `oauth.parse_usage` emits model-scoped windows

After the two account-wide windows, append one `QuotaWindow` per `limits[]` entry whose
`scope.model` is non-null, in payload order:

```python
QuotaWindow(key=f"{group}:{model_key}",          # "weekly:fable"
            label=f"{display_name} {group}",     # "Fable weekly"
            used_pct=float(entry["percent"]),
            resets_at=_parse_iso(entry.get("resets_at")),   # None allowed: fresh window
            duration_seconds=_SEVEN_DAYS_S if group == "weekly"
                             else _FIVE_HOURS_S if group == "session" else None,
            scope=SCOPE_MODEL,
            applies_to_models=(model_key,))
```

`model_key` is the lowercase tier token the server's `model_tier` produces for the same
model: from `scope.model.id` via `claude-(\w+)-` when the id is present, else the first
word of `display_name` lowercased (`"Fable"` → `"fable"`). An entry with neither is
skipped. Unlike the account-wide branch, a scoped entry with `resets_at: null` is
**still emitted** (0% used, nothing active) so the badge can show it; `QuotaWindow`
already models a None reset as "not perishable yet".

Fallback when the payload has no `limits` list at all: any top-level `seven_day_<x>`
object with a numeric `utilization` becomes `weekly:<x>`. This is the same fallback shape
the two account-wide windows already have.

`anthropic_snapshot(...)` gains a keyword-only `model_windows: Sequence[QuotaWindow] = ()`
appended after the account-wide pair. Account-scoped windows always come first — the
`binding_window` tie rule and `reserve_window` depend on provider order.

`QUOTA_BLOB_VERSION` stays at 2: the encoding already carries `scope` and
`applies_to_models`, and a lagging host's v2 reader decodes the extra window unchanged.
`_legacy_usage_fields` projects by key (`session`, `weekly`) and is unaffected.

### 2. `reserve_window` binds an account-scoped window

`QuotaSnapshot.reserve_window()` restricts its candidates to `SCOPE_ACCOUNT` windows
when any exist, so the reserve never attaches to `weekly:fable`. Same rule in the web
mirror `reserveWindow`. No behaviour change for Copilot (one window) or Codex (its
account-wide windows precede the per-limit ones and share the longest duration).

### 3. One definition of "the model a quota window is matched against"

`server/sessions/model_policy.py`:

```python
def quota_model(model: str | None, *, engine=AgentEngine.CLAUDE_CODE) -> str | None:
    """The key quota windows are matched against for a session that runs *model*.

    Claude: the tier of the model it will ACTUALLY launch on (`launch_model` resolves
    `default`/None to the inherited settings model), so "Default" sessions are judged
    on the fleet default the binary would pick. None when the tier is indeterminate
    (`best`, an unknown id) or the engine is not Claude — the selector then applies
    every window, the conservative direction.
    """
```

Every caller below uses this and nothing else; no call site re-derives a tier.

### 4. Thread the model through selection

- `selection.select_account(..., model: str | None = None)` →
  `accounts.pick_account_detailed(..., model=None)` → `select_picked(..., model=model)`.
  `pick_account` gains the same passthrough. The CLI and `launcher.py` keep passing
  nothing (unchanged, conservative).
- Orchestrator call sites pass `model=quota_model(<session model>, engine=<engine>)`:
  session spawn (`_spawn_session`, the request's model), the backlog healthy-account
  probe (the dispatch's model when known), `rotate_to_best`, `_rotate_away`. All four
  read the record or request that is already in scope.

### 5. Rotation monitor and dead-account checks judge per session

`_limiting_window(usage, ceiling, model=None)`, `_account_is_dead(account, model=None)`
and `_account_limited(account, model=None)` gain a `model` parameter and pass it to
`exhausted` / `at_ceiling` / `windows_at_or_over`.

`_rotation_monitor`:

- Account-level gate becomes "is **any** window at or over the ceiling" (model None,
  which includes scoped windows). If none: recovery branch as today.
- Per record on that account: `m = quota_model(record.model, engine=record.engine)`;
  `if not exhausted(usage, ceiling, extra_usage_allowed=..., model=m): continue`.
  A Sonnet session on a Fable-exhausted account is left alone. The pinned branch and the
  rotate branch use `_limiting_window(usage, ceiling, model=m)` for `paused_until` and
  the reason (`"Fable weekly limit reached"` — the window label already carries the
  model).
- `account_limited` notification: debounce key becomes `f"{account}:{window.key}"` so a
  Fable exhaustion and a later account-wide one each notify once; keys for windows that
  drop back under the ceiling are discarded on the next event. Message:
  `"<account>: Fable weekly limit reached"`.
- `_sweep_needs_attention` `limited` branch and crash recovery's `_account_is_dead` pass
  the record's `quota_model`.

### 6. Carry the SDK's `rate_limit_type`

`ev.RateLimit` gains `rate_limit_type: str | None`; the translator copies
`info.rate_limit_type`. `TurnFailure` gains `limit_type: str | None`
(`limitType` on the wire) and the driver fills it from the last `RateLimit` when it
signals a `rate_limit` failure. `_recover_session` logs it and appends it to the
rotation reason (`rate_limit:seven_day_opus`). Recovery policy is otherwise unchanged:
after §4 the rotation target is already picked for the session's model.

### 7. Web

`web/src/lib/usageWindows.ts` stays the single reader:

- `windowsFor(usage, model)` mirrors `windows_for`: model-scoped windows apply when
  `modelTier(model)` is in `appliesToModels`; a null tier applies every window.
- `bindingWindow(usage, model?)` filters through `windowsFor` first.
- `reserveWindow` prefers `scope === 'account'` (§2).
- `windowLabel(w)` returns the provider label for scoped windows and the existing
  abbreviation for account-wide ones; `SessionMeta.QUOTA_ABBR` falls back to it.

Consumers: `AccountsManager` already renders one badge per window, so the Fable badge
appears with no change beyond ordering (already provider order). `SessionCard` and
`useSessionRuntime` pass the session's model to `bindingWindow`, so a Sonnet session on a
Fable-exhausted account shows its real headroom. No new components.

### 8. Docs

`docs/product/features/usage-aware-rotation.md` gains a "Per-model windows" section:
the payload shape, the `weekly:<tier>` key contract, the conservative None rule, and
the three surfaces.

## Error handling

- A scoped entry with a malformed `percent` or no recognisable model is skipped, never
  raised: the account-wide windows still parse, and one odd entry must not blank an
  account's usage.
- `quota_model` never raises; an unreadable settings file already fails open to None
  inside `inherited_default_model`.
- All selection failure paths (`AllAccountsBusy`, `no_usable_accounts`) are unchanged;
  `earliest_reset` now naturally comes from the scoped window when that is what binds.

## Testing

Core (`tests/`, run with `PYTHONPATH=$PWD/src`):

- `test_oauth.py`: the verified 2026-09-14 payload (percentages only, no identifiers)
  yields `session`, `weekly`, `weekly:fable` in that order with scope/applies/duration;
  scoped entry with null `resets_at` still emitted; entry with no model skipped;
  `seven_day_opus` fallback without `limits`; an id like `claude-opus-4-8` maps to
  `opus`.
- `test_quota.py`: `reserve_window` ignores model-scoped windows; `binding_window("sonnet")`
  on the kindash shape returns `weekly` (91) not `weekly:fable` (100).
- `test_selector.py`: with the kindash snapshot, `rank_accounts(model="fable")` excludes
  the account and its `earliest_reset` is the Fable window's; `model="sonnet"` ranks it;
  `model=None` excludes it (conservative rule pinned by a test so it cannot drift).
- `test_accounts*.py`: `pick_account_detailed(model=...)` reaches `select_picked`.

Server (`server/tests/`):

- `test_quota_model.py`: `quota_model` table — `fable[1m]` → `fable`, `claude-fable-5-1`
  → `fable`, `opusplan` → `opus`, `best` → None, `default` → tier of the inherited model
  (patched settings file), Codex engine → None.
- `test_selection_model.py`: `select_account(model=...)` forwards to
  `pick_account_detailed`.
- Extend `test_dead_account_rotation.py`: fake poller publishes the kindash snapshot;
  a Fable session and a Sonnet session on that account → only the Fable session gets
  `_do_rotate`; the reason names `Fable weekly`; notification fires once for
  `account:weekly:fable`; pinned Fable session → `limited` with `paused_until` from the
  scoped window; `_account_is_dead(account, model="sonnet")` is False.
- `test_translator*.py`: `RateLimitEvent` → `ev.RateLimit.rate_limit_type`; driver's
  `TurnFailure.limit_type` on the wire.

Web (`web/`, full `vitest run`):

- `usageWindows.test.ts`: `windowsFor` / `bindingWindow(model)` / `reserveWindow` rules.
- `SessionCard` shows the account-wide weekly for a Sonnet session on the kindash shape.

Gates before push: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`,
`pytest tests/`, `pytest server/tests/`; in `web/`: `npm run typecheck`, `npm run lint`,
`npm run test`, `npm run build`.

## Risks

- **Fleet default is Fable.** Every "Default" session resolves to Fable via
  `launch_model`, so accounts at their Fable ceiling drop out for those sessions too.
  That is correct (they would 429), and Sonnet/Opus sessions and explicit picks are
  unaffected. The accounts page makes the reason visible.
- **Display-name mapping.** Anthropic names the scoped model by display name with a null
  id. If a future payload carries an id, the id wins; if it carries a display name that is
  not a known tier (e.g. a promotional bucket), the window is still emitted and applies
  only to that name: a Claude session with a known tier never matches it, while a
  model-blind caller (CLI, non-Claude engine) treats it as applying — the same
  conservative rule every scoped window gets. A test pins both halves.
- **Lagging host.** rocky's v2 reader decodes the extra window; its model-blind selector
  becomes conservative for that account until it takes this deploy. Same direction the
  new code takes for unknown models, so no host disagrees about a Fable pick.
