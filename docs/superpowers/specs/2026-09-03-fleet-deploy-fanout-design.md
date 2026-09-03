# Fleet deploy fan-out and runner version reconciliation — design

**Date:** 2026-09-03
**Status:** approved in chat (2026-09-03, all three decisions taken as recommended)
**Origin:** rocky ran a checkout 37 commits behind `origin/master` (parked on a hotfix
branch) for two days after PR #940 landed; the 22:07 EDT deploy restarted only dobby. The
session-side half of #940 (replay + titler) was therefore not live on the host that
actually runs sessions.

## Goal

1. A deployment updates **every online runner** and is reported complete only when each
   one has confirmed it is on the deployed commit.
2. A runner that was offline during a deploy brings itself to the deployed commit **as part
   of its own startup**.
3. The guarantee holds even when a step above fails: a runner that is not on the deployed
   commit **never receives new sessions**.

Non-goals: installing or changing systemd units on runners from the controller (still the
onboarding runbook); zero-downtime rolling restarts across runners; deploying to hosts
that are `enabled=false`.

## Today (what makes runners drift)

- `steps.restart_units` knows exactly one remote runner, named by
  `CCSWITCH_DAEMON_SSH_HOST` / `_REPO`, and touches it only when `daemon_changed`.
  Production does not set the variable, so no deploy has ever reached rocky.
- A second implementation, `sessions/host_upgrade.run_upgrade` (Fleet UI "update"
  button), fans a fetch → ff-only → pip → sync → restart sequence to one `Host` row over
  ssh and verifies `/info.sha`. It targets `origin/master` (not the deployed commit), does
  not drain, and keeps state in memory.
- `Host` rows carry `ssh_host`, `repo_path` and a probe-written `sha`; `host_probe` gates
  health on protocol only. A stale runner stays dispatchable.
- `deploy/ccswitch-daemon-remote.service` has no `ExecStartPre`; the daemon never checks
  its version at boot. `/info.sha` is `running_git_sha()`, the process's start-time HEAD.
- The controller (`server/deploy/controller.py`) has no per-host status and no DB access.

## Design

### 1. Durable "deployed version" — `deploy_state`

New single-row table (`id='singleton'`):

| column | meaning |
|---|---|
| `deployed_sha` | full sha of the last **successful** deploy (the edge is running it) |
| `deployed_short` | its short form as abbreviated on the edge |
| `daemon_sha` | full sha of the last successful deploy whose diff was `daemon_changed` |
| `deployed_at`, `deployed_by` | audit |

Written by the controller at the end of a successful deploy (after the edge health gate
and endpoint probes, before `resume`). `daemon_sha` advances only when that deploy was
daemon-changing. Read by the probe's skew gate (§4) and by runner startup (§3). Absent row
(fresh install) ⇒ no gate, no reconcile: fully backward compatible.

Migration `0016_deploy_state`; add the model to `_REQUIRED_EXTRAS` in
`test_postgres_compat.py` (NOT NULL `id`) per CLAUDE.md. `hosts` gains one nullable column,
`checkout_sha` (reconcile picks it up).

### 2. Deploy fan-out — the `runners` phase

Phases become `preflight, save, update, report, runners, restart, resume`. `save`,
`restart` (now **edge-only**) and `resume` keep their meaning; the new `runners` phase
owns every daemon in the fleet, including dobby's local one.

**Targets.** All `Host` rows with `enabled=true`. A row's most recent probe decides
whether it is reachable: `healthy=true`, or `healthy=false` with a *fenced* reason
(`last_error` starting `version skew` / `protocol mismatch` — the host answered, it is just
on the wrong code) ⇒ a deploy target (`runners.is_deployable`); `healthy` null (never
probed) or false for any other reason (unreachable) ⇒ status `offline`, skipped (§3 catches
it later). Amended at final review: treating fenced hosts as offline would have skipped a
runner forever after its first failed deploy. Rows are read from the DB — the controller
gains `server.db` access through the same `CCSWITCH_DB_URL` its env file already carries.

**Per-runner lifecycle** (`server/deploy/runners.py`, one `RunnerDeploy` per host, run
with a concurrency cap of **2**; decision 2a):

1. `snapshot_deploy` RPC on the host's own endpoint (best-effort, warning on failure).
2. **Update checkout to the target sha** — always, even on edge-only deploys, so every
   runner's checkout equals `deployed_sha`. Transport:
   - *local* (the host whose name is `local_host_stamp()` and has no `ssh_host`): nothing
     to do — the controller's `update` phase already moved this checkout.
   - *remote*: `ssh <ssh_host> git -C <repo_path> fetch --quiet origin`, then
     `merge --ff-only <target_sha>`, then pip via `<repo>/.venv/bin/python -m pip install
     -e '<repo>[server]' --quiet` **only if** `pyproject.toml`/`uv.lock` changed in
     `live..target` (the controller computes this once from its own checkout), then
     `sync`. Fast-forward only; a diverged runner fails this step.
   - A remote host with no `ssh_host`/`repo_path` ⇒ status `unconfigured` = failed. An
     online runner the deploy cannot reach is a guarantee violation and must be loud.
3. **Restart iff `daemon_changed`**: drain (`drain.drain_and_wait` against the host's own
   client, same caps), `close_events_connections`, then `sudo systemctl restart
   ccswitch-daemon.service` (local) / `ssh … sudo -n systemctl restart …` (remote).
   Edge-only deploys skip this: the allowlist in `steps._EDGE_ONLY` guarantees no
   daemon-imported Python changed, so the running process already equals the new
   checkout's daemon code.
4. **Verify**: poll `/info` until `checkoutSha` prefix-matches the target and — when
   restarted — `sha` (process) does too, within the existing 90 s budget. The next probe
   (≤30 s) records the new shas; the host stays healthy meanwhile, so no dispatch is lost.
   (Decided at final review: keeps `RunnerDeploy` DB-free and injectable.)
5. `restore_deploy` RPC (only if restarted), merged into `status.restore` per host.

**Status.** `DeployStatus` gains `hosts: dict[name, {state, step, error, sha}]` with
`state ∈ pending | running | done | failed | offline | disabled | unconfigured`, serialised
on `/status` and persisted like the rest. The phase is `done` only when every online host
is `done`; otherwise `failed` and `status.error` names the hosts (`runners: rocky failed at
merge: …`).

**Failure policy (decision 1a).** A failed runner does **not** roll the edge back and does
not stop the other runners: the edge restart and the health gate still run, the deploy is
reported failed with the host named, and the skew gate (§4) fences that runner until an
operator fixes it (the existing per-host upgrade button now targets `deployed_sha`, see
§5). Rollback on an **edge** health failure keeps today's semantics and additionally
rewinds (`reset --hard <previous sha>`) every runner this deploy had already moved, so the
fleet never ends split-version in the rollback case either.

**Consolidation (constitution VI).** `steps.restart_units` loses its ssh branch and
becomes `restart_edge()`; `host_upgrade._remote_commands` becomes the single remote
update sequence, parameterised by target sha and "deps changed"; the Fleet API's
`upgrade_host` calls the same `RunnerDeploy` with `deployed_sha` as target.
`CCSWITCH_DAEMON_SSH_HOST/_REPO` are read once at edge startup to seed the matching
`Host` row's `ssh_host`/`repo_path` when those are null, then ignored; the runbooks stop
documenting them as the deploy path. `remote_daemon_configured()` becomes "any enabled
remote host exists" (it only sizes the health window).

### 3. Startup reconcile — `server.deploy.runner_sync`

`deploy/ccswitch-daemon-remote.service` (and the local unit) gain
`ExecStartPre=<venv>/bin/python -m server.deploy.runner_sync`. Runs as the service user in
the checkout, with the unit's env file (so `CCSWITCH_DB_URL` is present). Behaviour:

1. Read `deploy_state.deployed_sha` with a raw SQL statement (never the ORM model — the
   script runs on the *old* checkout, which may predate the model). No row ⇒ exit 0.
2. `git rev-parse HEAD` equals it ⇒ exit 0 (the common case, sub-second).
3. Otherwise, only if the working tree is clean: `git fetch --quiet origin` (60 s
   timeout), `git checkout -q master`, `git merge --ff-only <deployed_sha>`. Then pip
   (`-m pip install -e '.[server]' --quiet`) iff `pyproject.toml`/`uv.lock` differ between
   old HEAD and the target, then `sync`.
4. Any failure — dirty tree, diverged master, no network, pip error — is logged to the
   journal at ERROR with the exact reason and **exits 0** (decision 3a): the daemon starts
   on whatever it has (rehydrating its sessions) and the skew gate keeps it out of
   dispatch. Refusing to start would strand that host's sessions for a problem only an
   operator can fix.

Bootstrapping: the first rollout on an existing runner is manual — the runbook's reset
recipe (`checkout master && reset --hard origin/master`) plus installing the unit with the
new `ExecStartPre`. After that the mechanism is self-sustaining.

### 4. The guarantee — version-skew gate in `host_probe`

`/info` reports two shas: `sha` (process, `running_git_sha()`, unchanged) and
`checkoutSha` (fresh `git rev-parse --short HEAD`, cached ≤10 s). The probe stores both.
Next to the protocol gate:

```
skew if deploy_state exists and (
    not short_sha_match(checkout_sha, deployed_short)
    or not is_ancestor(daemon_sha, process_sha)      # process predates the last daemon-changing deploy
)
→ healthy=False, last_error="version skew: checkout=<a> process=<b> deployed=<c>"
```

`is_ancestor` runs against the edge's own checkout (`deploy.git.is_ancestor`), memoised
per `(daemon_sha, process_sha)` so the 30 s probe pays git once per generation. An old
daemon that reports no `checkoutSha` falls back to `sha` for the first check. Everything
that reads `Host.healthy` (`fleet._dispatchable`, the Fleet UI, `/api/fleet/hosts`)
inherits the gate; no dispatch-side change is needed.

### 5. Operator surface

- Deploy page: a `Runners` row in `DEPLOY_PHASES` (`web/src/lib/deployPhases.ts`) with
  per-host sub-rows from `deploy.hosts` (name, state, step, short sha); `allPhasesDone` /
  `deployOutcome` treat a failed host as a failed deploy.
- Fleet section: shows `checkoutSha` beside `sha`, and the skew `lastError` renders like
  the protocol mismatch does today. The "update" button targets `deployed_sha`.
- Runbooks: `rocky-session-host.md` §5 and `onboarding-a-session-runner.md` step 7/8
  describe the fan-out, the `ExecStartPre`, the one-time bootstrap, and drop the env-var
  deploy path.

## Data flow (daemon-changing deploy, one remote runner)

```
controller: preflight → save → update(edge checkout) → report(daemon_changed=True)
  runners: [dobby-local]  snapshot → drain → restart → verify(sha,checkout) → restore
           [rocky]        snapshot → ssh fetch/ff/pip?/sync → drain → ssh restart → verify → restore
  restart(edge) → wait_healthy → endpoints → deploy_state := target (daemon_sha := target)
  resume → publish
probe (every 30 s): /info {sha, checkoutSha} vs deploy_state → healthy / "version skew"
runner boot: runner_sync reads deploy_state → ff to it → daemon starts on the new code
```

## Error handling summary

| failure | outcome |
|---|---|
| runner offline at deploy time | skipped (`offline`), reconciles at its next boot |
| online runner without ssh config | `unconfigured` ⇒ deploy failed, host fenced by skew gate |
| ssh/merge/pip fails on a runner | that host `failed`, others continue, edge lands, deploy reported failed |
| runner restarted but never reports target sha | `failed` after 90 s, same as above |
| edge health gate fails | rollback: edge reset + every already-moved runner rewound, as today |
| `runner_sync` cannot reconcile | logged at ERROR, daemon starts, host fenced until fixed |
| `deploy_state` row missing | no gate, no reconcile (fresh install / pre-migration) |

## Testing

- `test_deploy_controller.py`: runners phase ordering (runners before edge), per-host
  status shape, offline skipped, unconfigured fails, one failed host ⇒ error + edge still
  restarted, rollback rewinds moved runners, `deploy_state` written only on success,
  `daemon_sha` advances only when daemon-changing, concurrency cap honoured.
- `test_fleet_upgrade.py` / `test_deploy_remote_restart.py`: rewritten against the single
  remote sequence (explicit target, conditional pip, ff-only never resets outside
  rollback, argv injection rejections unchanged).
- New `test_runner_sync.py`: no row / equal / ff / dirty / diverged / fetch timeout / pip
  paths, all via injected `run`; exit code always 0 except on usage errors.
- `test_host_probe_skew_gate.py`: match, checkout behind, process older than
  `daemon_sha`, missing `checkoutSha` fallback, no `deploy_state`.
- Web: `deployPhases` tests for the new phase and host-failure outcome; FleetSection
  renders `checkoutSha` and the skew error.
- Postgres suite lists updated for the new table.
