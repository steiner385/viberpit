# Host platforms: route `env: windows` work to a runner with a Windows VM

Status: design, awaiting approval · 2026-11-03

## Intent

steiner385/ghostdriver-mcp is a Windows desktop-automation MCP server. Its `env: windows` issues
(42 to date, 2 open: #257, #248) need a live, logged-on Windows desktop. The Backlog Runner skips
them, because `FLEET_PLATFORMS = {"linux"}` (`server/pm/queue.py:132`, #2983/#3001): the issues
used to be dispatched to Linux hosts that could not do the work.

The operator decided to keep rocky on Linux as a session runner and host a Windows 11 VM on it
(KVM/libvirt). This design makes ViberPit aware of that VM: rocky advertises that it can run
Windows work, and the runner sends `env: windows` issues only to rocky, one at a time.

## Operator decisions

- **Rocky stays Linux.** A KVM Windows 11 VM runs beside the daemon. The daemon itself stays Linux;
  native-Windows daemons stay out of scope.
- **Desktop UI automation is the requirement.** Sessions must run ghostdriver's tests and UI work
  inside the VM's logged-on interactive desktop, not just a shell.
- **One Windows session at a time** per host. The VM has one desktop, so concurrent sessions would
  collide.
- **Full runner hookup.** Future `env: windows` issues flow automatically, with no manual
  dispatch.

## Goals

1. A runner declares extra platforms it can serve (for rocky: `windows`). The edge learns them
   through the same `/info` → probe → `Host` row path as engine advertisement
   (`server/sessions/host_engines.py`).
2. The queue's environment gate uses the platforms that enabled, healthy hosts actually
   advertise, instead of the constant. With no Windows host, behaviour is unchanged: `env: windows`
   is still skipped with reason `environment`.
3. A dispatch for an `env:`-gated issue may land **only** on a host that advertises a matching
   platform. This is a hard filter, like `engine`, never a `prefer`. If none is available, the
   session is not created on another host.
4. At most one live session per (host, non-Linux platform). Further issues wait in the queue
   under a distinct skip reason, instead of failing.
5. A session routed for `windows` knows the VM exists and how to use it, through one helper
   that runs a command in the desktop and returns its output and exit code.

## Non-goals

- A native Windows ViberPit daemon. The POSIX dependencies are listed in the investigation,
  and WSL2 was not chosen.
- `real-target` issues. They stay skipped, because they need a specific app in the operator's
  own session, which a VM cannot vouch for.
- A platform picker in the manual launch UI. Manual launches can already pick host `rocky`.
- macOS hosts. The mechanism is platform-generic, but only `windows` is exercised.
- Several VMs per host, or VM cloning.

## Anti-goals: a failure even if it "works"

- An `env: windows` session lands on dobby or any host that cannot reach a Windows desktop. This
  is the #2983 regression.
- A down or unhealthy Windows host blocks or slows Linux backlog work.
- Two sessions drive the VM desktop at once.
- Secrets (the VM password, the ssh private key) reach a transcript, a log line or the repo.

## Constraints

- Constitution v1.2.0. In particular VI (one source of truth: extend the engine-advertisement
  pattern, don't fork it) and III (verify against the real VM, not assumptions).
- DB: new columns are nullable, with an Alembic revision chained off `0096_session_views`. If a
  model or table is added, update the Postgres lists (`_REQUIRED_EXTRAS`, `RECONCILE_EXEMPT`).
- Rolling compatibility: a daemon that omits `platforms` is treated as `{"linux"}`. That is the
  only place this default is applied, mirroring `LEGACY_ENGINES`.
- New settings use the `VIBERPIT_` prefix in both `Settings` classes in `server/config.py`.

## The design

### 1. Advertisement (daemon → edge)

- Add a new daemon setting, `VIBERPIT_HOST_PLATFORMS`: comma-separated and validated against the
  known OS names in `_ENV_PLATFORMS`. `linux` is always included.
- `/info` gains `platforms: [...]`, written next to `engines` in `server/ipc/daemon.py`.
- The edge probe (`host_probe.py`) persists it into a new nullable JSON column, `Host.platforms`,
  on every outcome, just as it does for `engines`.
- `HostCandidate` gains `platforms: frozenset[str]`, filled in `FleetRouter.candidates()`. A
  missing value means `{"linux"}`.
- **Builder's call:** one module owns build, parse and verdict (a `host_platforms.py` sibling of
  `host_engines.py`, or a generalisation of it). Either way, the writer and reader share one key
  constant.

### 2. The queue gate

- Async callers that rank already hold a DB session: the runner pool at `orchestrator.py:~8420`,
  `GET /api/pm/queue`, `pm_tree`, escalation, and `escalation_approval`. They compute the fleet's
  platform union from enabled, healthy `Host` rows and pass it to
  `needs_unavailable_environment(platforms=…)`. `skip_reason`, `rank` and `skipped_by_policy` gain
  a `platforms` keyword that defaults to `FLEET_PLATFORMS`, so pure tests are unchanged.
- **Busy platform:** add a new skip reason, `platform_busy`, to `SKIP_REASONS`. It applies when
  every host that could serve the issue's gated platform already holds a *working* session (call 1) routed for
  that platform. It sits after `environment`: "no host can" wins over "the host is busy". The
  board and the queue API show it like any other skip reason.
- `FLEET_PLATFORMS` stays as the default, and its comment changes to say the live set comes from
  host advertisement.

### 3. Routed dispatch

- Labels ride along with the dispatch. The issue's gated platform, worked out once by a new
  helper beside `needs_unavailable_environment` that returns the platform or None, is carried on
  the selected `Batch` → `_backlog_spawn` → `spawn_session` → `_automation_create_session` →
  `InternalCreateSessionRequest.platform` → `FleetOrchestrator.create_session(platform=…)` →
  `FleetRouter.pick(platform=…)`.
- `rank_hosts` filters by platform before scoring, the same way it filters by `engine`. The
  capacity-queue path (`_queue_hosts`) applies the same filter, so a queued windows session can
  only drain onto a windows host.
- A batch never mixes platforms. A gated issue is dispatched as a singleton batch. **Builder's
  call** where to enforce this in backlog selection.
- Add a nullable `DbSession.platform` column, stamped at create time. It is the source for the
  `platform_busy` count, and it is what the daemon reads to decide whether to inject the guide (§4).
- **Fallback when the router is disabled:** if `automation_host_routing_enabled` is off, there is
  no routed path. A platform-gated dispatch must then refuse rather than spawn locally. **Builder's
  call** whether to refuse at selection or at spawn.

### 4. What a Windows session is told and given

- **Host setup, outside the daemon:**
  - The VM is reachable from rocky as `ssh win11` through a host alias in `~/.ssh/config` with the
    dedicated key `~/.ssh/win11_ed25519`.
  - A helper, `winrun`, runs a command inside the logged-on desktop. Its source is checked in under
    `scripts/winvm/` and installed onto rocky's PATH. The mechanism is a one-shot interactive
    scheduled task as the auto-logged-on user, with stdout/stderr and the exit code captured to
    files and copied back.
  - A companion, `winsync`, mirrors the session's worktree into the VM.
  - **Builder's call** on sync mechanics (tar over ssh, or git bundle) and on the exact CLI shape.
    It must return the remote exit code faithfully and never echo secrets.
- **Session guidance:** the host config names a platform guide file per platform, for example
  `VIBERPIT_HOST_PLATFORM_GUIDE_WINDOWS=/etc/viberpit/windows-guide.md`. For a session with
  `platform == "windows"`, the daemon appends that guide to the session instructions through the
  single composition point `session_instructions` (`server/sessions/tools/instruction.py:119`), so
  Claude and Codex get the same text. Non-Windows sessions on rocky do not get it.
- The guide (checked in as a template under `docs/runbooks/`) says three things: the VM exists,
  use `winsync` then `winrun`, and the desktop is shared state that must be left clean.

### 5. The VM (host provisioning, done in this session; recorded as a runbook)

- **VM:** libvirt domain `win11` on rocky: q35, UEFI Secure Boot, swtpm TPM 2.0, 4 vCPU, 8 GB,
  100 GB qcow2 on virtio, NAT network `default`, autostarted.
- **Windows:** Windows 11 Pro installed unattended. Activation comes from rocky's OEM key, through
  the firmware MSDM table passed into the VM.
- **Desktop:** local user `viber` with auto-logon. The password is in
  `~/.config/viberpit/win11-vm.secret` (0600) on rocky.
- **SSH:** OpenSSH server with key-only login and PowerShell as the default shell.
- **Always on:** no sleep, no lock screen, and no automatic reboot while a user is logged on.
- **Tooling:** Python 3.12 and Git for Windows, to match ghostdriver's CI (`windows-latest`, 3.12).
- **Runbook:** `docs/runbooks/windows-vm-on-a-session-runner.md` covers rebuild, snapshot
  ("clean desktop") and restore, and the licensing note.

## Calls made by the designer (not operator decisions; check these)

1. **The VM is held only while a session is working.** `platform_busy` counts sessions routed
   for that platform whose status is in the working set (a turn running or queued to run), not
   sessions that have handed back, are waiting on CI or review, or are done. A handed-back Windows
   session must not stall the backlog for days.
2. **The hard guarantee is a lock inside `winrun`, not the queue.** Ranking-time `platform_busy`
   is advisory, because ticks race. `winrun` takes an exclusive host-local lock for the whole
   desktop command (`flock` on rocky). A second caller waits, up to a bounded timeout, then fails
   with a clear message. This is where two sessions would actually collide, and it also covers
   manual launches, forks and anything unstamped.
3. **Advertising `windows` depends on a live VM.** The daemon includes `windows` in `/info` only
   when a cached reachability check passes: `ssh win11` with a short timeout, re-checked at most
   every 60 s, run off the `/info` request path. A stopped VM drops the platform, and gated issues
   go back to `environment` skips instead of failing sessions.
4. **`env: both` is not gated.** It runs anywhere, as today. Only labels whose platforms exclude
   `linux` route to a platform host. The helper returns None for `both` and `any`.
5. **Windows dispatch is Claude-only.** Codex's workspace-write sandbox has no network egress, so
   it cannot reach the VM. A platform-routed dispatch forces engine `claude`, or refuses if the
   product pins Codex. No sandbox exception is added.
6. **Secrets: accepted risk, same as today.** Sessions on rocky run as `tony`, who already holds
   stronger credentials (gh, Claude, Codex). The VM key only reaches a local VM on rocky's NAT
   network. No privilege separation in v1. `winrun` never prints the password or key, and the
   guide tells sessions not to read either file.
7. **Platform inheritance.** `DbSession.platform` is inherited on fork, child sessions and
   respawn (resume or model switch), so the guide is re-applied. Manual sessions on rocky are
   unstamped and protected only by the `winrun` lock (call 2).
8. **API and UI surface.** `GET /api/fleet` gains `platforms` per host. `platform_busy` renders
   through the existing generic skip-reason display. Any web label map and the assistant tool's
   reason list are updated in the same change.

## Verification (must be shown, not assumed)

- **Unit tests:**
  - parse and build of `platforms`, including the legacy absent value.
  - `rank_hosts` / `pick` platform filtering, including the capacity-queue path.
  - `needs_unavailable_environment` with an advertised union.
  - the `platform_busy` reason.
  - a gated batch stays a singleton.
  - the guide is injected only for `platform == "windows"`.
- **Regression:** with no host advertising `windows`, the #2983 tests still pass unchanged.
- **Real end to end, against rocky and the VM:**
  1. Set `VIBERPIT_HOST_PLATFORMS=linux,windows` on rocky.
  2. `GET /api/fleet` shows the platforms.
  3. `GET /api/pm/queue` no longer skips ghostdriver #248 for `environment`.
  4. A dispatched session lands on rocky and runs ghostdriver's `pytest` through `winrun` in the
     VM desktop.
  5. A second `env: windows` issue shows `platform_busy` while the first is live.

## Left to the builder

The `host_platforms` module shape, where singleton-batching is enforced, refuse-at-selection vs
refuse-at-spawn when routing is off, `winrun`/`winsync` internals, and test fixture shape.
