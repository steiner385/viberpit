# Deploy Monitor: GitHub deployment statuses → incidents, alerts and issues

Status: design, 2026-09-12. Written and executed in an autonomous session; the owner's request
was "implement a monitor capability in viberpit for deployment failures … GitHub … would ideally
be our source of truth". Decisions the owner did not pre-approve are marked **(decision)**.

## 1. Why now

On 2026-09-12 KinDash's Railway deploy of the admin console failed three pushes in a row
(`aa67b5b3`, `450c8792`, `32f834cb`); `admin.kindash.com/health` kept serving the last good build
(`59f80f99`). Nobody was told. GitHub *did* know: Railway wrote a `FAILURE` Deployment status on
each `kindash / admin` deployment, and the `deploy-admin` workflow run concluded `failure`. But
KinDash's workflow deliberately emits `::warning::` instead of filing an incident issue for the
admin service, so no issue existed and nothing in ViberPit looked at deployment statuses.

The GitHub Deployments API is therefore the right source of truth for "is a deploy failing":
Railway writes it itself, it is per environment, it carries the commit and a log URL, and it needs
no Railway credentials.

## 2. Goal and non-goals

**Goal.** For every repo ViberPit manages, watch each deploy environment's newest GitHub
deployment. When an environment is *failing* (newest deployment `FAILURE`/`ERROR`) or *stalled*
(newest deployment not terminal for longer than a grace period), open one durable incident,
alert once through the notification rules, and — in `file` mode — file exactly one GitHub issue
in that repo, keep it updated while the incident lasts, and close it on recovery. GitHub stays the
record: an open `deploy-monitor` issue means an ongoing incident.

**Non-goals.**

- Talking to Railway. No Railway token, no Railway webhooks. If the platform stops writing
  Deployment statuses the monitor goes quiet, and §7 says how that is visible.
- Health-URL polling. `server/pipeline/deploy_watch.py` already polls health endpoints for the
  Pipeline timeline; this monitor reads GitHub only. Joining the two is a follow-up.
- Auto-remediation. No fix session is dispatched. A read-only *diagnose* action (Control-Band
  Monitor's `diagnose` tier) is the obvious next step and is listed in §10.
- A new nav section. The automation card (status, feed, config, actionable list) is the whole UI
  **(decision)**: it is what Error Triage and the CI/CD Doctor ship with, and it needs no web
  work beyond one notification-class label.

## 3. What exists today

| Piece | Where | State |
|---|---|---|
| Deployment node fields + per-env GraphQL query builder | `server/github/pr_status.py` (`_DEPLOY_NODE_FIELDS`, `build_env_deployments_query`) | built, reads `latestStatus.state` only |
| Env-name classifier (`qa`/`prod`/`admin`) | `server/github/deploy_envs.py:classify_env` | built, the one classifier |
| "Managed repos" resolution | `orchestrator._cicd_list_repos(config)` | built, reused as-is |
| File / comment / regress / skip rule | `server/automations/issue_filing.py` | built |
| Issue writes over `gh`, issue-state sync, close | `server/github/ci_issues.py` | built |
| Durable per-target state | `AutomationContext.get/set/delete_target_state` | built |
| Notification routing, event classes | `server/sessions/notify_rules.py:EVENT_CLASSES`, `push.notify_event` | built, no deploy class |
| Automation chassis (registry, primary-only loops, card UI) | `server/automations/`, `orchestrator.start()`, `web/src/features/delivery/` | built |

## 4. Architecture

```
primary daemon, every 120 s
  DeployMonitorLoop.tick()
    ├─ list_repos(config)            ← orchestrator._cicd_list_repos (existing)
    ├─ fetch_deployments(repos)      ← one GraphQL doc, newest 50 deployments per repo
    ├─ evaluate(env, deployments)    ← pure: healthy | failing | stalled | unknown
    ├─ reconcile per (repo, env)     ← AutomationTargetState "repo#env"
    │     new incident → emit info + notify_event("deploy_failed") [+ file issue]
    │     ongoing      → comment on issue at most every comment_every_h
    │     recovered    → emit info, close issue with a comment, delete state
    └─ stash counts                  → DeployMonitor.status() on the card
```

Three units, each testable alone:

1. **`server/github/deployments.py`** — pure. `Deployment` dataclass, `build_deployments_query(repos)`,
   `parse_deployments_response(data) -> dict[str, list[Deployment]]`. Imports
   `_DEPLOY_NODE_FIELDS` from `pr_status` (which gains the extra fields, §5.1) so the two
   readers can never drift in what a deployment node is.
2. **`server/automations/deploy_monitor.py`** — `DeployMonitor` (contract surface), pure policy
   (`evaluate_env`, `incident_key`, issue text), `DeployMonitorLoop` (injected seams), and
   `DeployMonitorActionProvider`.
3. **Wiring** — `registry.py`, `orchestrator.py` (ctor, `start()`, `stop()`,
   `_PRIMARY_ONLY_LOOP_ATTRS`, `automation_statuses`, action providers), `notify_rules.py`,
   `assistant/watches.py`, `NotificationsSection.tsx`.

## 5. Detail

### 5.1 Deployment fetch

`_DEPLOY_NODE_FIELDS` becomes:

```
databaseId createdAt latestEnvironment commitOid ref { name } creator { login }
latestStatus { state createdAt logUrl environmentUrl }
commit { committedDate }
```

Existing readers (`_active_deploy_dates`) ignore the extra fields. `build_deployments_query`
aliases one `repository(owner, name) { deployments(orderBy: CREATED_AT DESC, first: 50) }`
block per repo, JSON-encoding names like `build_pr_query`. 50 covers ~16 KinDash merges (three
deployments each). Parsing is total: a node missing `latestEnvironment` or `commitOid` is
dropped; a null repo block (App permission forbidden) yields an empty list for that repo, and
the loop emits one `warn` per tick-run for it (§7).

`Deployment`: `env`, `sha`, `ref`, `creator`, `created_at`, `status_state` (upper-case, `""` when
`latestStatus` is null), `status_at`, `log_url`, `env_url`.

### 5.2 Evaluation (pure)

```python
@dataclass(frozen=True)
class EnvVerdict:
    env: str
    kind: str            # healthy | failing | stalled | unknown
    newest: Deployment
    streak: int          # consecutive newest-first deployments sharing the failing kind
```

`evaluate_env(env, deployments, *, now, grace_min, stall_min) -> EnvVerdict`:

- deployments are that env's, newest-first; `newest` is the first.
- `failing` when `newest.status_state in {"FAILURE", "ERROR"}` **and** `now - newest.status_at
  >= grace_min` (default 5). The grace absorbs KinDash's automatic 30 s retry, whose second
  deployment would otherwise race the tick. `streak` counts leading deployments with a failing
  status.
- `stalled` when `newest.status_state in {"PENDING", "QUEUED", "WAITING", "IN_PROGRESS", ""}`
  and `now - newest.created_at >= stall_min` (default 30). `streak` is 1.
- `healthy` when `newest.status_state in {"SUCCESS", "INACTIVE"}` — `INACTIVE` is what GitHub
  writes on an older success when a newer one lands, so it is a healthy signal for the env.
- `unknown` otherwise (e.g. a young failure inside the grace window); the loop treats `unknown`
  as "no change" — it neither opens nor closes an incident.

Environments are filtered to `classify_env(env) is not None` unless `all_envs` is on, so preview
environments never open incidents.

### 5.3 Incident state

Target `f"{repo}#{env}"` in `AutomationTargetState`:

```
{kind, sha, first_sha, first_seen_at, last_seen_at, streak, log_url,
 issue_number, filed_at, last_commented_at, last_commented_sha,
 notified: bool, muted: bool}
```

Transitions per tick for each `(repo, env)`:

| state row | verdict | action |
|---|---|---|
| none | failing/stalled | create row; `emit("info", kind="incident")`; `notify_event("deploy_failed", …, repo=, url=, fanout_when_unconfigured=True)`; in `file` mode, file issue |
| exists | failing/stalled, same `kind` | update `sha`, `streak`, `last_seen_at`; in `file` mode with an open issue, comment at most every `comment_every_h` when `sha` changed since the last comment |
| exists | failing ⇄ stalled | update `kind`; treated as ongoing (no second alert) |
| exists | healthy | `emit("info", kind="recovered")`; if the row holds an issue that is not already closed, comment naming the recovering sha and close it — in **any** mode, because the issue exists only because `file` mode or an operator's *File issue* click asked for it; delete row |
| exists | unknown | untouched |
| exists, muted | any non-healthy | update measurements only; no comment, no notify |
| exists | env vanished from an otherwise-successful read, or the repo left the watched set | `emit("info", kind="vanished")` once, guarded by `vanished_noted` (cleared if the env reappears); row is left as-is — no delete, no measurement update, since there is no fresh deployment to measure |

The incident event is `info`, not `warn`: `AutomationContext.emit` fans `warn`/`error` through
`automation_warn`/`automation_error` too, so emitting the incident at `warn` would double-push a
wildcard notification rule — one push from that fan-out and one from `notify_event("deploy_failed")`
**(decision)**. `warn` stays reserved for the loop's own trouble (`fetch_failed`, `file_failed`,
`close_failed`).

Issue-state sync (learning that a human closed the issue) runs for a repo whenever one of its rows
holds an open issue, regardless of mode — it is a read, and it does not skip a *muted* row: a
manual `mute` action (unlike a `not_planned` close) leaves `issue_closed` false, so without this a
human closing that issue would never be learned and recovery would try to close it again. The row
also carries `cap_noted` (the daily-cap event was already emitted for this incident, so it is not
repeated every tick), `regression_of` (the issue number a pending file is regressing, so a failed
regression create does not lose the link on retry), and `vanished_noted` (see the table above).

The alert fires **once per incident** (`notified` guards it, and never at all while `muted`, even
if the first notify failed before the mute); `notify_event` uses `fanout_when_unconfigured=True`
like `pr_broke` **(decision)**: a failing deploy is the class of event a user with no rules
configured still expects to hear about.

### 5.4 Issue filing (mode `file`)

Reuses `issue_filing.decide/apply` with `Linked(number, closed, closed_reason,
last_commented_at)` and a per-day cap `daily_cap` (default 10). The day's count is derived from
`filed_at` on the live incident rows (a row claimed by a failed create counts too, as in Error
Triage), so a daemon restart does not re-arm the cap — but the row being filed excludes ITSELF
from that count: a retried claim re-stamps its own `filed_at` every attempt, so counting it too
would starve a retry sitting at the cap's last slot.

- no issue → `file`. Title `[deploy-monitor] <env> deploy failing — <repo> @ <sha10>`
  (`stalled` substitutes the word). Label `deploy-monitor`. Body: table (repo, environment,
  kind, newest sha + ref, first failing sha, consecutive failures, first seen, creator, log URL,
  environment URL) plus a footer `Target: repo#env`. Text from GitHub is passed through
  `neutralize_markers`.
- issue open → `comment` (throttled by `comment_every_h`, default 6) — only when the failing
  sha moved, so a static failure does not chatter.
- closed `not_planned` → mute until recovery (row `muted=true`).
- closed `completed` → acknowledged for the sha failing at close time; if a *newer* sha is still
  failing → `regress`: file a new issue linking the old one. (Refiling on the same sha would
  punish a human who closed the issue knowing a fix was on its way.)

Issue state is learned with `ci_issues.issue_states` on rows that carry an `issue_number`. Filing
is two-phase like Error Triage: the row is written before the `gh` call, `issue_number` after it,
so a failed create is retried next tick and never double-files.

Recovery closes the issue via `ci_issues.close_issue` after `comment_on_issue("Recovered: <env>
is serving <sha10> as of <ts>")`. Close failures are logged and retried next tick (the row is
deleted only after a successful close, or immediately in `watch` mode).

### 5.5 Modes and config

`modes = ("off", "watch", "file")`. `watch` detects, feeds and alerts but never *opens* an issue
or comments on one; `file` adds the issue lifecycle. An issue that already exists on a row (filed
earlier in `file` mode, or via the card's *File issue* action, which is offered in `watch` too) is
still closed on recovery in either mode, so an open `deploy-monitor` issue always means an
ongoing incident. `engine_policy = CLAUDE_ONLY` — it never spawns a session.

| key | type | default | help |
|---|---|---|---|
| `enabled_repos` | repo-multiselect | `[]` | empty = every managed repo |
| `all_envs` | bool | `false` | also watch environments `classify_env` does not recognize |
| `grace_min` | int (min 0) | 5 | how long a failed status must stand before it counts |
| `stall_min` | int (min 1) | 30 | how long a non-terminal deployment may run |
| `comment_every_h` | int (min 1) | 6 | recurrence comment gap |
| `daily_cap` | int (min 0) | 10 | new issues per day |

Loop cadence 120 s (module constant, like every other automation).

### 5.6 Card surface

- `status()`: `"{n} repos · {m} envs watched · {k} incident(s)"`, plus `" ({m} muted)"` when any
  are muted. Level `error` when any **non-muted** incident is `failing`, `warn` when only
  `stalled`, `active` when watching with none, `idle` when off — a muted incident counts toward
  the total but never drives the level, since it is deliberately silenced.
- `actionable()`: one item per incident — title `"<repo> · <env> <kind>"`, subtitle with sha,
  streak and age, `url` = issue URL if filed else log URL. Actions: `file_now` (when no open issue,
  mode is not off, and the row is not muted), `mute` (risky) / `unmute`, and `dismiss` (risky, on
  every row): deletes the row, best-effort commenting "Dismissed by operator…" and closing any
  issue that is not already closed, in any mode.
- Feed kinds: `incident`, `recovered`, `filed`, `regressed`, `commented`, `closed`,
  `close_failed`, `file_failed`, `cap`, `fetch_failed`, `fetch_recovered`, `muted`, `unmuted`,
  `vanished`, `dismissed`.

### 5.7 Notification class

`EVENT_CLASSES` gains `deploy_failed`. `assistant/watches.py` gains
`"deploy": {"fails": ("deploy_failed", "scope_repo")}` so the assistant can say "tell me when a
KinDash deploy fails". `NotificationsSection.tsx` gains the `Deploy failed` label.

## 6. Data flow across the process boundary

Nothing new crosses it. Status goes through `automation_statuses()` (ctx map entry
`"deploy-monitor"`), actionable items and triggers through the existing `_action_providers`
map, config through the generic automations API.

## 7. Error handling

- A tick never raises (`tick()` wraps `_run_once` like Error Triage).
- GraphQL failure for the whole batch → `emit("warn", kind="fetch_failed")` once per tick and
  no state changes: existing incidents are neither opened nor closed on missing data.
- A repo whose deployments block came back null → skipped for the tick, one `warn` per tick.
- `gh` write failure → row stays retryable; `file_failed` event.
- Invalid repo string (`ci_issues.valid_repo`) → skipped with a log line.

## 8. Testing

TDD, all in `server/tests/`:

- `test_github_deployments.py` — query builder (JSON-encoded names, empty list → `""`), parser
  (drops malformed nodes, null repo block → empty, upper-cases state).
- `test_deploy_monitor_policy.py` — `evaluate_env` for each verdict, grace/stall windows,
  streak counting, env filtering, issue title/body (length cap, marker neutralization).
- `test_deploy_monitor_loop.py` — injected `fetch_deployments`/`run`/`clock`/`notify`; covers
  new incident (one alert, one issue), ongoing (throttled comment on sha change only),
  recovery (comment + close + row gone), mute via `not_planned`, regress via `completed`,
  watch mode writes nothing, fetch failure changes nothing, daily cap, retry of a failed create.
- `test_deploy_monitor_wiring.py` — registry entry, `_PRIMARY_ONLY_LOOP_ATTRS`, `stop()` reap,
  action provider and status-ctx entries (mirrors `test_error_triage_wiring.py`).
- `test_notify_rules.py` / `test_assistant_watches_api.py` — `deploy_failed` is a member and is
  actually emitted.
- Web: `npm run typecheck && npm run lint && npm run test` (the label is data only).

## 9. Rollout

Ships disabled (`ensure_rows` default). Enable on the primary as `watch` first, confirm the
feed shows `cairnea/KinDash · kindash / admin failing`, then switch to `file`. The first `file`
tick will open the KinDash issue that should have existed on 2026-09-12.

## 10. Follow-ups (not in this change)

- `diagnose` action: spawn a read-only diagnosis session for an incident, reusing
  `server/sdlc/prompts.py:build_diagnosis_prompt` and the Control-Band spawn cap.
- Join with `DeployWatcher` health polling so a `SUCCESS` deployment whose health endpoint
  never picks up the sha is also an incident.
- Per-product policy hook (`server/automations/product_policy.py`) to choose mode per product.
