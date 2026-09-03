---
slug: deploy-controller
title: Self-Hosted Deploy Controller
state: live
links:
  code:
    - server/deploy/controller.py
    - server/deploy/drain.py
    - server/deploy/steps.py
    - server/deploy/git.py
    - server/deploy/serve.py
    - server/deploy/types.py
    - server/api/deploy.py
  prs: [853, 868, 885, 870, 727, 788, 719, 481]
---

# Self-Hosted Deploy Controller

**Problem** — ccswitch is self-hosted and continuously improves itself, so it must upgrade the
running edge (web server) and session daemon to a new commit without killing the live `claude`
sessions it hosts. A naive `git pull` + `systemctl restart` kills in-flight turns, can ship a
half-broken tip, splits the edge and daemon onto different code versions, and — when the upgrade
touches deploy code itself — the controller has to restart into new code mid-deploy without
reporting a phantom failure. Roughly 47 deploy PRs churned this into a controller that treats its
own upgrade as a first-class, drain-safe, rollback-safe operation.

**Target users** — the single operator running their own ccswitch instance, who deploys new commits
from the web UI (or on a schedule) and expects live sessions to survive it.

**Desired outcome** — one-button (or automatic) upgrade to the newest CI-green commit that drains
in-flight turns first, restarts edge and daemon together onto the same code, health-gates on real
functional endpoints (not just `/healthz`), rolls both units back to the last-good commit on any
failure, and survives the controller's own self-restart with the deploy status intact — all while
`/status` stays responsive so the UI never freezes.

**Success metrics** — no live turn is SIGKILLed by a deploy (drain waits up to the turn/condense
caps first); edge and daemon always run the same SHA after a deploy or rollback (never
split-version); a red tip does not block deploying an older green commit; a failed health gate rolls
both units back to the pre-deploy SHA; a controller self-restart mid-deploy reports a terminal
outcome rather than a frozen spinner.

**Non-goals** — a general multi-environment CD pipeline (this deploys one self-hosted instance);
container/orchestrator-based deploys (it drives `git` + `systemd` directly); zero-downtime for the
edge HTTP layer itself (browsers reconnect after the edge restart — it is the *sessions* that are
protected); deploying to hosts outside the fleet registry (the fan-out targets the edge plus every
registered `Host`, and nothing else).

**Notes** —
- **Phased deploy** (`server/deploy/controller.py`), seven phases: PREFLIGHT (fetch, validate CI
  green, check target ancestry) → SAVE (snapshot live sessions) → UPDATE (ff git, sync deps via
  `python -m pip`, rebuild frontend) → REPORT → RUNNERS (fan out to every fleet daemon) → RESTART
  (the edge: drain, restart units, health-gate) → RESUME (reattach dropped sessions). RUNNERS runs
  *before* the edge restart — daemon-before-edge is load-bearing (#190). Every synchronous step runs
  via `asyncio.to_thread` so the loop keeps serving `/status` and `/healthz`.
- **Draining in-flight turns** (`server/deploy/drain.py`): `drain_and_wait` asks the daemon to
  `begin_drain`, polls `drain_status`, and waits until safe. Ordinary turns are capped at
  `CCSWITCH_DRAIN_TURN_CAP_SEC` (default 180s); a session mid-compaction (`condensing`) is
  uninterruptible and protected up to `CCSWITCH_DRAIN_CONDENSE_CAP_SEC` (default 570s). Returns
  `ready`/`cap`/`unavailable`. Backed by the daemon's SIGTERM drain (PR #853) so even a bare
  `systemctl restart` no longer kills sessions.
- **Newest-green targeting** (PR #885): when `origin/master`'s tip is red, `POST /api/deploy/start`
  accepts an explicit `targetSha` (the newest green pending commit). Preflight validates it is a hex
  SHA, an ancestor of `origin/master`, and reachable from the current checkout by fast-forward;
  `ci_verdict` confirms green against the repo's *required* branch-protection contexts only.
- **Health gate + functional probe** (`server/deploy/steps.py`, PR #727): `wait_healthy` polls
  `/healthz` for `status:ok` and the expected short SHA, and also gates on the daemon (rejects
  `daemon:"unavailable"`; matches `daemonSha` when the daemon changed). Because `/healthz` never
  reads the ledger or session store, `require_endpoints_healthy` additionally probes
  `/api/sessions`, `/api/accounts`, `/api/github/repos` (a corrupt ledger once left `/api/accounts`
  500ing while `/healthz` stayed green). Window widens to ~150s when a remote daemon is present.
- **Rollback** (`rollback_to`): on any post-UPDATE failure, `git reset --hard` to the pre-deploy
  SHA, best-effort frontend rebuild, and restart *both* units together so they run the same old
  code; a remote daemon is *rewound* (not fast-forwarded) to that SHA (PR #788 — a prior bug
  fast-forwarded the remote to the very commit being rolled back).
- **Surviving self-restart** (`server/deploy/serve.py`, PR #868): `DeployStatus` is persisted
  atomically to `/run/ccswitch/deploy-status.json` after every phase. When deploy code changes, the
  controller schedules a deferred `systemd-run` restart so it runs after the status is persisted. On
  boot, a `running=True` status from a died-mid-deploy predecessor is reported as a terminal failure
  rather than a frozen spinner.
- **Fleet fan-out** (`server/deploy/runners.py`, PRs #719, #708): the RUNNERS phase deploys to
  **every** host in the fleet registry, not to one env-configured remote. Each `Host` row supplies
  its own `ssh_host`/`repo_path`; per runner the lifecycle is snapshot → fetch → `merge --ff-only`
  → pip (only when `pyproject.toml`/`uv.lock` changed) → `sync` → (drain → `sudo -n systemctl
  restart ccswitch-daemon.service`, only on a daemon-changing diff) → verify `/info` → restore. Two
  runners run at once and one runner's failure never stops another; the local daemon reuses the
  controller's own checkout and restarts without SSH. A host the probe could not reach is reported
  `offline`, but one merely *fenced* by the version-skew or protocol gate is still deployed to —
  the deploy is what un-fences it. `CCSWITCH_DAEMON_SSH_HOST`/`_SSH_REPO` are no longer a deploy
  path: they only seed the matching host row's `ssh_host`/`repo_path` once at edge startup. The
  `sync` is load-bearing: ext4 delayed allocation once left a git-rewritten `orchestrator.py` as
  zero bytes after an unclean reboot (PR #815). A runner also self-reconciles at boot via
  `ExecStartPre=… -m server.deploy.runner_sync`.
- Earlier hardening: PR #481 stopped systemd SIGKILLing the edge on every deploy; PR #870 made
  restore outcomes observable and replays interrupted turns on resume; `restore` from the SAVE
  snapshot drives the RESUME reattach.

**Evidence** —
- `server/deploy/controller.py` — phased `DeployController`, rollback trigger, self-restart scheduling.
- `server/deploy/drain.py` — `drain_and_wait` turn/condense caps.
- `server/deploy/steps.py` — `ci_verdict`, `wait_healthy`, `require_endpoints_healthy`, `rollback_to`,
  change classification (`classify_change`/`_EDGE_ONLY`).
- `server/deploy/runners.py` — per-runner fan-out lifecycle, `is_deployable`, rollback `rewind`.
- `server/deploy/runner_sync.py` — boot-time reconcile (`ExecStartPre` on the daemon unit).
- `server/deploy/serve.py` — durable `DeployStatus` path + interrupted-deploy recovery.
- `server/deploy/git.py`, `server/deploy/types.py` — git ancestry helpers, status/phase model.
- `server/api/deploy.py` — `/api/deploy/status`, `/api/deploy/start` (`targetSha`).
- PRs: #853 (SIGTERM drain), #868 (durable status across self-restart), #885 (newest-green target),
  #870 (observable restore + replay), #727 (functional endpoint probe), #788 (rollback rewinds
  remote), #719 (update remote daemon code over SSH), #481 (stop SIGKILLing the edge).
