---
slug: usage-aware-rotation
parent: accounts-and-engines
title: Usage-Aware Account Rotation
state: live
links:
  code:
    - src/ccswitch/selector.py
    - src/ccswitch/oauth.py
    - src/ccswitch/ledger.py
    - src/ccswitch/runner.py
    - src/ccswitch/launcher.py
    - server/usage/poller.py
    - server/api/accounts.py
  prs: ['#558', '#537', '#531', '#534', '#540', '#539', '#680', '#90']
---

# Usage-Aware Account Rotation

Keep coding work moving by choosing accounts with usable quota and rotating when an account reaches its limit.

**Problem** — Even with several accounts registered, a user still has to watch each account's 5-hour
and weekly usage windows, guess which one has the most headroom, and manually switch when one hits a
limit mid-task. Getting this wrong wastes perishable quota (capacity that resets unspent) or stalls a
session at a hard ceiling.

**Target users** — Anyone running long or bursty Claude work across a multi-account pool: headless
scripts (`ccrun`), interactive sessions (`ccmax`), the web session daemon auto-rotating live
sessions, and shells pinned via `ccswitch use`.

**Desired outcome** — The system tracks per-account session/weekly usage percentages and reset
times, automatically picks the account with the most usable (most-perishable) headroom, and rotates
to another account the instant one hits its limit — waiting for the earliest reset only when every
account is exhausted. Soft reservations spread back-to-back launches across different accounts so
they don't all pile onto the same one. This is the project's core value proposition (README line 3:
"Automatic Claude Max account rotation to stay within session/weekly usage limits").

**Success metrics** — (unknown — needs owner input). Grounded candidates: fraction of turns
completed without a user-visible limit stall; perishable quota wasted per week (the selector's
`_perishability` score is the explicit optimization target); how evenly load spreads across accounts
under back-to-back launches; time-to-recover a session after its account hits 429/exhaustion.

**Non-goals** — Splitting one agent view's sub-agents across accounts (inherit the view's account by
design). Rate-limit backoff is NOT owned by the poller — it delegates to the ledger's shared
usage-fetch cooldown (`server/usage/poller.py` docstring), so this capability deliberately excludes
per-caller backoff logic.

**Notes** —
- Selection (`selector.py`): accounts at/above the ceiling are excluded, the rest are ranked by
  `_perishability` — quota-per-hour that would be wasted before the binding (session or weekly)
  window resets — so near-reset accounts are preferred and fully-fresh accounts saved for last.
  `AllAccountsBusy` carries the earliest reset and a `kind` (`at_ceiling` vs usage-unreadable vs
  needs-reauth, #239) so the edge renders a clear message across the RPC boundary.
- Soft reservations: `ledger.reserve`/`reservation_active` place a short hold on a just-picked
  account so running `ccswitch use` back-to-back for several terminals spreads them out (README).
- Weekly reserve (#558, meaning fixed by spec 2026-09-20 D4): a per-account `weeklyReservePct` is
  the share of the weekly allowance KEPT BACK (the capacity governor's push level supplies the
  default: conserve 40 / steady 20 / max 5). Once weekly usage reaches `100 − reserve`, the
  account is unavailable to the governor and, for an explicit override, becomes a soft fallback
  here — burned least-weekly-used first, only after every unreserved candidate is exhausted
  (`rank_accounts`). The push level's default is never applied to a human's own pick.
- Auto-rotation on limit: `runner.py` (`ccrun`) detects a limit in the result, adds the account to
  `tried`, prints "rotating to another account", and re-picks; with `--wait` it sleeps until the
  earliest reset. `launcher.py` backs `ccmax` (interactive, auto-switch + `--resume`). The web
  daemon self-heals live sessions off dead/exhausted accounts (#531, #532, #534).
- Usage source of truth: `oauth.usage_for` hits Anthropic's OAuth usage endpoint with retry/backoff
  and Cloudflare-403 handling; results are cached in the shared ledger with a shared fetch cooldown
  (#539, #540). `server/usage/poller.py` fans usage changes out to subscribers and counts
  consecutive failures as a rotation-monitor input. Copilot usage is mapped onto the same `Usage`
  dataclass so the selector ranks it unchanged.

**Evidence** — Ranking/perishability/`AllAccountsBusy` in `src/ccswitch/selector.py`; usage fetch +
retry in `src/ccswitch/oauth.py`; reservations, reserve thresholds, and usage cache in
`src/ccswitch/ledger.py`; CLI rotation loops in `src/ccswitch/runner.py` (ccrun) and
`src/ccswitch/launcher.py` (ccmax); async poller in `server/usage/poller.py`. PRs: #558
(per-account weekly reserve), #537 (stop sole-headroom starvation, surface AllAccountsBusy), #531
(self-heal sessions on dead/exhausted accounts), #534 (auto-recover needs_attention on free-up),
#540/#539 (ledger-shared usage-fetch backoff), #680 (short-circuit on 400 refresh failure), #90
(session/weekly window labels + reset tooltips).

## Per-model windows (2026-09-14)

Anthropic meters some models separately from the account-wide 5-hour and 7-day
windows. The usage payload carries them as `limits[]` entries with
`kind: "weekly_scoped"` and `scope.model.display_name` (the `id` is null); e.g. an
account can read weekly 91% but Fable 100%.

- **Parsing.** `oauth.parse_usage` emits one `QuotaWindow` per scoped entry after the
  account-wide pair: key `"<group>:<tier>"` (`weekly:fable`), `scope = "model"`,
  `applies_to_models = ("fable",)`. The tier token is what
  `server/sessions/model_catalog.model_tier` yields for the session's model.
- **Selection.** `select_account(..., model=quota_model(...))`: the selector applies
  only the windows that meter that tier, so a Fable-exhausted account still serves
  Sonnet. A caller that names no model (CLI launchers, non-Claude engines, `best`)
  gets **every** window applied — conservative by design.
- **Rotation.** The poller-driven monitor moves only sessions whose model the
  exhausted window meters; the reason and the `account_limited` push name the window
  ("Fable weekly limit reached"), once per account+window.
- **Display.** Every window is a badge on the Accounts page; session cards bind on
  the session's own model (`lib/usageWindows.ts:windowsFor`).
- **Not done here:** downgrading a session's model when every account is exhausted
  for it (a policy decision), per-model reserves, Codex per-model selection.
