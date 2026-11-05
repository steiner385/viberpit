# D-17 "Move session to host": verification spike

Date: 2026-11-05. Constitution III: every claim below was checked in one of three ways: against the installed binary,
by a no-model probe, or in ViberPit's code and the live hosts (read-only). Claims that were not checked are listed
under **Gaps**.
Parent spec: `2026-11-01-ui-desktop-parity-design.md` line 215 (the "Move to host" row, `[A-D19]`: "Pushes the
branch, then resumes the session on the target fleet host, keeping the session id and transcript. The source
worktree is kept until the move is confirmed.") and line 435 (`D-17 | Move session to host`). Benchmark:
`docs/audits/2026-11-01-claude-desktop-code-benchmark.md:178` ("Move session to cloud / other host", ❌) and owner
decision D19 at `:288`.
Earlier spikes: `2026-11-04-d16-verification.md` (format and harness).

**The headline finding: D-17 already has a design, and part of it has shipped.**
- `2026-09-14-cross-host-session-transfer-design.md` is a full design for exactly this operation (status: "draft —
  awaiting Tony's review"). Its plan is `docs/superpowers/plans/2026-09-14-cross-host-session-transfer-phase1.md`.
- Only its PR 1a landed: `src/viberpit/worktree_transfer.py` (#1701, 2026-09-14), with tests in
  `tests/test_worktree_transfer.py`.
- Nothing calls the module outside tests. A grep of `server/` and `src/` finds no importer, so today it is unused
  code (Principle VI).
- No server, RPC, API or UI piece exists: no `moving_to` column, no `transfer_*` RPC, no `cross_host_transfer`
  capability, no `SESSION_MOVING` frame and no "Move to host" item.
- `git ls-remote origin 'refs/viberpit/transfer/*'` on `steiner385/viberpit` is empty, so the transfer path has never
  run against GitHub.

So D-17 should **adopt and update the 2026-09-14 design, not write a second one**. §6 lists the facts in it that have
since gone stale.

## 0. Versions, hosts and probes

- **Binary (dobby).** `~/.local/bin/claude` resolves to `versions/2.1.318` (sha256 prefix `708c77b204db3008`, the
  file D-14 and D-16 used). Every `@N` offset below is a byte offset into this file.
- **Binary (rocky).** Live worker sessions run `versions/2.1.293` (sha256 prefix `8968405e26db478a`). A copy was taken
  by `scp` (read-only on rocky) and run **on dobby** for the probes. Nothing was written on rocky.
- **Codex.** `codex-cli 0.153.2` (`/usr/bin/codex`). The app-server JSON schema came from
  `codex app-server generate-json-schema` into a throwaway dir.
- **Fleet at the time** (`GET 127.0.0.1:8787/api/fleet/hosts`):
  - dobby: primary, 5/5 sessions;
  - rocky: worker, 4/6 sessions;
  - both at sha `a3146965`.
  All 12 listed sessions were Claude. Their `cwd`s were under `/home/tony/GitHub/viberpit-sessions/worktrees/` on both
  hosts.
- **Resume probes** (`/tmp/d17-scripts/capture.py`):
  - `env -i`, with a throwaway `HOME` and `CLAUDE_CONFIG_DIR` under `/tmp`, and a dummy API key.
  - `ANTHROPIC_BASE_URL` pointed at a 30-line local HTTP stub. The stub records the `messages` array of each
    `/v1/messages` request and answers 400. **No request left the host and no model ran.**
  - The probe dirs, the copied binary and a copied real transcript were all deleted afterwards. No real account dir was
    written.

## A. Session ownership today

### A.1 How the owning host is recorded and routed

- **One stamp.** `sessions.host` (`server/db/models.py:247`) is "Fleet host that ran this session, stamped at
  creation by the dispatcher … Never inferred at read time".
  - **There is no `daemon_epoch` column on `sessions`.** Epochs exist only per host: `HostHeartbeat.epoch`/`fence`
    (`models.py:1564-1576`).
  - Per-session exclusion is a **lease row**, not a column: `daemon_leases` keyed `session:<id>`
    (`server/sessions/session_lease.py:51-80`, the `DaemonLease` model at `models.py:1534`).
- **The edge routes by the stamp, through a cache.**
  - `FleetOrchestrator._host_for` (`fleet_orchestrator.py:372`) reads `sessions.host` into `self._host_cache`
    (`:295`).
  - `_make_per_session` (`:393`) sends each `_PER_SESSION` method (`:202-248`: `get_session`, `send_prompt`,
    `ensure_driver`, `archive_session`, `reload_session`, `fork_session`, `rewind_session` …) to
    `router.get(host)`. On `UnknownHost` or a NULL stamp it **falls back to the primary** (`:396-399`).
  - `subscribe` (`:1094`) uses the cache only; it never reads the DB.
- **The cache is written in three places and never invalidated by a stamp change.** Writers:
  - `_host_for` (`:383`);
  - `_dispatch` (`:518,520`);
  - `list_sessions` (`:875`), which pops an entry only when a worker that answered no longer lists the id (`:883-887`).

  A restamp done outside the edge process keeps routing to the old host until that self-heal fires or the edge
  restarts. This is the #1432 failure; it was closed, but the cache behaviour is unchanged.
- **Each daemon decides ownership.**
  - A worker rehydrates only rows stamped with its own name (`orchestrator.py:19702`).
  - The primary loads every row (it serves the dashboard mirror) and gates its *acting* loops on `_owns_record`
    (`:25217`).
  - `_owns_record` says yes when a live driver is in this process, or by `_owns_record_by_stamp` (`:25237`). The stamp
    rule: a worker owns `host == me`. The primary owns NULL, its own name, `"local"`, and any stamp that has **left the
    enabled registry** (`:25256-25260`).
  - **`_owns_record` reads the in-memory `record.host`, not the row.** Only queued rows are re-synced from the DB
    (`_resync_moved_queued_mirrors`, `:28623`).
- **Every driver build is gated.** `_claim_session` (`:3435`) runs before every driver build:
  - layer 1 refuses a record stamped for another enabled host (`:3471-3489`);
  - then it takes the lease, TTL `VIBERPIT_SESSION_LEASE_TTL_S` = 120 s (`:912`);
  - `adopt=True` skips layer 1, for "adopt_session, transfer import" (`:3462`).
  - **Layer 4.** `_reconcile_foreign_lease` (`:3675`) closes a live driver whose row is stamped for another enabled
    host. It never does so mid-turn, and it has **no adopt bypass**: "an adopting path must stamp the row AND
    `_records[sid].host` … before its driver is registered" (`:3686-3688`).
- **Leases exist only while a driver does.** `_session_lease_tick` (`:3566`) releases the lease of any session with no
  driver (`:3613-3615`). **An idle, detached session holds no lease at all.**

### A.2 Placement of a new session, and overriding it

- `FleetOrchestrator.create_session` (`fleet_orchestrator.py:405`) → `FleetRouter.pick` (`fleet_router.py:157`):
  - `rank_hosts`/`score_host` (`server/sessions/fleet.py:119,103`) filter on enabled, healthy, capacity, engine and
    platform, then score on headroom, memory and load.
  - `_dispatch` (`:492`) tries hosts best-first, stamps the winner (`_stamp_host`, `:685`) and never cascades past an
    outcome-unknown timeout (`:499-508`).
- **Override.** `POST /api/sessions` takes `host` → `prefer=` (`api/sessions.py:1039`). The UI is the DispatchBar
  Runner select (`web/src/features/sessions/list/DispatchBar.tsx:182`, `''` = Auto).
  - `prefer` **reorders but keeps failover** (`fleet_router.py:185-189`).
  - A client sitting at a fleet host's desktop prefers that host (`fleet_orchestrator.py:434-444`).
  - The assistant is pinned to the primary with no failover (`:428-430`, `:477-489`).
- **A child session is created in-daemon** (`orchestrator.py:23786`, `self.create_session`, not the fleet), so it
  always lands on its parent's host. Forks (D-8) also run on the source's host.

### A.3 Existing move / adopt / rehome operations

| Mechanism | What it moves | Cross-host? | Evidence |
|---|---|---|---|
| Queue adoption | a **queued** (never-started) row: another host's drain restamps and starts it | **yes**, but only before any worktree exists | `fleet_orchestrator.py:591-623`; `_resync_moved_queued_mirrors` `orchestrator.py:28623-28671` |
| `adopt_session` | an **untracked local** `claude` process: kills it, resumes in place, `adopted_in_place=True` | no (scans this host's `/proc`) | `orchestrator.py:23179`; `server/sessions/adopt.py:1-8` |
| `reload_session` / `_reassign_locked` | rebuilds the driver on the **same** host, `resume=<engine_session_id>` (bounded interrupt, close, re-claim, start) | no | `orchestrator.py:22383`, `:26824` |
| Split-brain repair | `archive_session` on the holder daemon, then unarchive and `ensure_driver` on the owner | manual runbook (memory note), no product op | lease reconcile `:3675` |
| Dead-host adoption (#949) | spec 002 Phase 4, "fenced adoption & cross-host resume" | **not built.** Issue #949 is OPEN; `host_heartbeat.declare_dead` (`host_heartbeat.py:123`) has **no caller** | grep |
| `worktree_transfer` | the worktree, through git transfer refs | building block only; no caller | `src/viberpit/worktree_transfer.py` |

- **`StaleOwnership` is automation ownership** (`server/automations/ownership.py`), not host ownership. Execution is
  keyed on **session id**: `_automation_before_execution` `orchestrator.py:2720-2771`, `begin_session_execution`.
- A non-terminal rebuild already attests "stopped" for the old driver (`_automation_driver_closed`, `:2774`). **A move
  that keeps the session id is, to the ownership layer, a rebuild like Reload or rotation.** PR Doctor and Backlog
  Runner claims survive it, and no `StaleOwnership` follows.

### A.4 When the owning host is down

- **Listing.** `list_sessions` skips a worker that does not answer (`fleet_orchestrator.py:845-850`).
  `reachable_names` (`fleet_router.py:80`) stops asking hosts the probe marked unhealthy, so the session shows from
  the primary's mirror as detached. `liveness` (`api/sessions.py:408`; `server/sessions/liveness.py:8-18`) shows
  `stale` or `unreachable`, never `dead`, because nothing declares death.
- **Every per-session action** goes to the dead host and fails with `DaemonUnavailable`.
- **Disabling the host does not free its sessions.**
  - If the operator disables it and the edge restarts, `build_fleet_router` drops it (`fleet_orchestrator.py:275-280`),
    and routing falls back to the primary (`:396-399`). The primary owns the orphaned stamp (`orchestrator.py:25260`).
  - But `_ensure_session_cwd` refuses to build: the worktree is missing and the row names another host
    (`reason="foreign_host"`, `orchestrator.py:6706-6709`). This is deliberate (#2913: "rebuilding a copy here is the
    2026-09-07 incident itself").
- **So today a dead host's sessions are stranded until it returns.** Its leases lapse at 120 s, but the worktree is
  the blocker. That is #949's scope; D-17 should not take it on (§E).

## B. What has to move

### B.1 Worktree

- **Same path on both hosts.** Worktrees live at `<repo_root>/viberpit-sessions/worktrees/<repo>/<short-id>`
  (`src/viberpit/worktrees.py:12`, `worktrees_base` `:410`, `session_worktree_dir` `:478`).
  - `repo_root` is `/home/tony/GitHub` on both hosts. Read-only `ls`: both have
    `~/GitHub/viberpit-sessions/worktrees/{KinDash,keel,viberpit,…}`.
  - **A moved session's `cwd` is therefore byte-identical on the target**, and so is its transcript project slug.
  - rocky still holds legacy `~/GitHub/.ccswitch/worktrees/*` trees, and dobby has none.
    `worktree_transfer.import_` always recreates under the **new** base (`worktree_transfer.py:200`), so a legacy-path
    session's `cwd` would change on a move (§B.2).
- **Clones are per host and local.**
  - Both hosts keep repos under `~/GitHub/.ccswitch/clones/<owner>/<name>` (`gitrepos.clone_path`, `gitrepos.py:378`).
    For example, rocky `…/worktrees/KinDash/e69af506` → `--git-common-dir` `/home/tony/GitHub/.ccswitch/clones/cairnea/KinDash/.git`.
  - `~/GitHub` on rocky is local `/dev/sda2`.
  - A worktree's `.git` file points into its own host's clone, so the directory itself is not portable (copying it
    would not work).
- **The worktree is host-local only.** dobby has no `…/KinDash/e69af506` or `…/viberpit/c52767f2` (both live on
  rocky).
- **Branches.** Sessions push their own branches as part of their work. ViberPit pushes nothing on a session's
  behalf. The parent spec row says "pushes the branch", but pushing a session's real branch fires `push`-triggered CI
  and PR automation. The transfer ref namespace below avoids that.
- **What `worktree_transfer` already does** (`src/viberpit/worktree_transfer.py`):
  - `inspect` (`:79`): dirty count (`git status --porcelain`, ignored files excluded), branch, HEAD, unpushed authored
    branches.
  - `export` (`:125`):
    - refuses a dirty tree without `carry_dirty` (`:139-140`);
    - otherwise `git add -A` plus a `--no-verify` WIP commit (`:144-154`);
    - one `--atomic` push of `+<head>:refs/viberpit/transfer/<sid>/head` and each unpushed authored branch
      (`:155-170`);
    - undoes the WIP commit if the push fails.
  - `import_` (`:189`):
    - fetches the namespace and recreates the worktree at the canonical path;
    - **refuses `branch_busy_on_target`** instead of the `create_worktree` child-branch rename (`:224-227`);
    - unpacks the WIP with `reset --mixed`, so the edits arrive unstaged and the branch tip is unchanged (`:239-242`);
    - is idempotent for restore-to-source (`:210-214`).
  - `finalize` (`:252`): resets the WIP on the source, then `remove_worktree(force=True)`.
  - `delete_transfer_refs` (`:270`).
- **Coverage and limits.**
  - Covered: tracked staged and unstaged changes, and untracked non-ignored files (all through `git add -A`).
  - **Lost: staging boundaries** (everything arrives unstaged) and **ignored files** (`node_modules` links, `.venv`,
    build output). These are rebuilt the way any fresh worktree is.
  - **Stashes do not travel.** `refs/stash` belongs to the clone, not the worktree (rocky's viberpit clone has 2
    entries shared by every session on it), so a session's stash cannot be told apart from another session's.
  - **Recovery refs do not travel.** `refs/viberpit/recovery/<sid>/…` (`worktrees.snapshot_recovery_ref` `:1184`) are
    local, unpushed refs; this session's own worktree has 5 on dobby.
- **Alternatives considered.**
  - `git bundle` relayed through the edge would avoid putting WIP on GitHub, but it needs a new byte transport over
    RPC (daemons never talk to each other).
  - The fork code's untracked-file copier (`create_fork_worktree`, `worktrees.py:939`) is same-host only.

### B.2 Conversation

- **Where the transcript lives.** `<CLAUDE_CONFIG_DIR>/projects/<slug(cwd)>/<engine_session_id>.jsonl`:
  - the slug replaces `/`, `.` and `_` with `-` (`orchestrator.py:24949-24956`);
  - `projects` is in `SHARE_ALLOWLIST` (`src/viberpit/configdir.py:32`), so each account dir links it to
    `~/.claude/projects`.
- **It is shared across hosts, physically on dobby.**
  - rocky mounts `~/.claude` and `~/.claude-accounts` from dobby (`findmnt -t fuse.sshfs` on rocky:
    `tony@100.102.11.121:/home/tony/.claude`, where `100.102.11.121` is dobby's tailnet IP). The mounts come from
    `viberpit-mounts.service`, which the daemon `Requires=`.
  - `ls ~/.claude | md5sum`, the `projects/` entry count (2617) and `history.jsonl` size and mtime are identical on
    both hosts.
  - Every live rocky session's `<engine_session_id>.jsonl` was found **on dobby's local disk** at its cwd slug (6/6
    checked).
  - **The whole per-account config dir is the same directory on both hosts**, not only `projects/`:
    `~/.claude-accounts/cfg/<acct>` is the sshfs tree.
- **Resume by id ignores the cwd (probe P1, 2.1.318 and 2.1.293).**
  - Setup: a two-message transcript ("remember the word PAPAYA" / "OK, PAPAYA.") written under
    `projects/-tmp-d17-probe-a/<sid>.jsonl`, then `--resume <sid>` run from cwd `/tmp/d17-probe/b` (a different slug).
  - `-p` and `--input-format stream-json` (the SDK's mode) behave the same: `init` carries the same `session_id`, and
    the **first `/v1/messages` request carries the PAPAYA exchange**.
  - The CLI **appended to the original file** under slug `a` and created no file under slug `b`.
  - A bogus id gives `No conversation found with session ID: …` (`@301092831`; `not_found_explicit_id` `@301091736`).
  - `--resume` also accepts a `.jsonl` **path** (`--resume file unreadable` `@301092053`, `Unable to load transcript
    from file` `@301092173`). This is an explicit fallback if the id lookup ever becomes cwd-scoped.
- **Version skew (probe P2).** A real dobby transcript written only by 2.1.318 (11 conversation lines) was resumed by
  rocky's 2.1.293 from another cwd. Both versions sent **n=11** messages in the first request.
- **ViberPit's own lookup is cwd-tolerant but slow off the fast path.** `_find_transcript` (`orchestrator.py:24958`)
  stats the cwd-derived path first, then globs `projects/*/<id>.jsonl`. That glob is ~2617 directories over sshfs from
  rocky, the scan that once took more than 200 s (2026-08-21; comment at `:24972-24977`). **Keep `cwd` identical
  across a move.** Moving a legacy `.ccswitch` worktree would change it and fall onto the glob.
- **So `--resume <engine_session_id>` works on the other host with nothing to copy.** #1432 already did this by hand,
  and the 2026-09-14 spec rests on it. P1 and P2 make it reproducible without a model call.
- **Codex.**
  - Thread history lives in the runner's own `CODEX_HOME`, which is refused on network filesystems and through symlinks
    (`server/codex/process.py:18-27`, `ensure_codex_home` `:180`).
  - On rocky the unit sets `XDG_STATE_HOME=/var/lib/viberpit/state`.
  - ViberPit is forbidden to read rollout or SQLite state (`server/codex/replay.py:4-12`; design D8, cited at
    `orchestrator.py:2600`).
  - `ThreadResumeParams` (0.153.2 schema) takes `threadId`, `cwd`, `model` … and has **no `path` or `history`
    field**, so a thread can resume only on the host whose `CODEX_HOME` holds it.
  - **Codex sessions cannot move with their history** (§E D2).

### B.3 ViberPit state

| State | Where | On a move |
|---|---|---|
| `sessions` row (`host`, `cwd`, `branch`, `engine_session_id`, `model`, `permission_mode` …) | shared DB | restamp `host`; everything else is unchanged when the path is identical |
| `daemon_leases session:<id>` | shared DB | source releases (`_release_session_lease` `orchestrator.py:3551`, epoch-stale, `primary_lease.py:124-140`); target claims with `adopt=True` |
| Edge `_host_cache` | edge memory | **must be rewritten by the move** (§A.1) |
| Source `_records[sid]` | source daemon memory | worker: pop. **Primary: keep as a mirror but set `rec.host = target`**, or `_owns_record` (in-memory stamp) lets the primary's loops resume it locally (§C.3) |
| Target `_records[sid]` | target memory | **absent on a worker** (rehydrate runs only at boot), and `ensure_driver` returns None for an unknown id (`:22686-22688`); needs a per-row load through `_record_from_row` (`:19602`, already the single mapper) |
| Driver, events ring, `_last_prompt`, `_pending_prompt_preambles` (`:1197`), `_pending_initial_prompt`, `_self_model_state` | source memory | lost, exactly as on a daemon restart. The ring refills from the transcript on attach. Carry pending preambles in the manifest (one list) |
| Pending permission / question cards | driver memory | none at a turn boundary; a mid-turn move would orphan them (`orphaned_asks.py` re-offers from the transcript) |
| Background work (`background_work`, `pending_rearm`, `models.py:137,145`) | source processes | **dies with the driver.** `waiting_external` sessions have it by definition; the rearm machinery (`rearm_background_tasks` `:19962`) relaunches after a restart and could relaunch on the target |
| Rewind undo (`session_rewinds.backup_dir`, `.host` `models.py:707`: "only it can undo") | source disk | expire the latest rewind (stamp `expired_at`), as the next prompt does |
| File checkpoints (D-9) | `file-history`, shared and sshfs | moves for free |
| MCP health (`mcp_server_status` keyed `(account, host, server)` `models.py:1579`) | DB, per host | target re-reports; nothing to move |
| Virtual X display / native display (`display_mode`) | per host | virtual: torn down by `_teardown_driver_state` (`:6538`), recreated on demand. **Native: bound to the source's screen, so refuse** |
| Queue entry (`session_queue`) | DB | queued sessions already move by restamp (§A.3) |
| Automation ownership (`automation_operations`, `models.py:800`) | DB, keyed by session id | unchanged (§A.3) |
| Attachments (`attachments_dir()/<sha>`) | edge | past turns are in the transcript; nothing to move |

### B.4 Accounts and credentials

- **Claude.** The primary daemon on dobby is the **sole credential authority** (`server/sessions/claude_authority.py:1-9`).
  - The edge pushes short-lived envelopes to every worker every 30 s with a 120 s lease
    (`fleet_orchestrator.push_credentials` `:931`; `CREDS_PUSH_INTERVAL_S`/`CREDS_LEASE_S`
    `src/viberpit/credential_envelope.py:34-35`).
  - Every account is therefore usable on every healthy host. A move needs no credential step and adds no second
    authority (Constitution II, `constitution.md:56-75`).
  - The account itself is unchanged by a move. Rotation stays orthogonal.
- **Copilot.** The proxy runs per host and per account and is spawned on demand (`copilot_proxy.py`), so it works on
  either host.
- **Codex.** The authority home sits on the authority host, and each runner has its own home (Constitution II, "separate
  host-local runner homes"). Auth is available per host, but **history is not** (§B.2).

## C. Constraints and risks

### C.1 Mid-turn

- **A move must be idle-only.** The source's driver must close for the target's `--resume` to be the only writer of
  the JSONL. `ensure_driver` already waits for an in-flight release, for this reason: the SDK warns that a concurrent
  resume "can lose the last assistant message" (`orchestrator.py:22696-22710`).
- The existing gate is `status ∉ MID_TURN_STATUSES` (`{in_progress, condensing, retrying}`, `server/sessions/status.py:47`)
  and `∉ INACTIVE_SESSION_STATUSES` (`models.py:34`), checked again **inside** the `_swapping` slot so a prompt cannot
  land between check and close.
- **`waiting_external` also needs a rule.** It is not mid-turn, but it has live background shells or Monitors, and a
  move kills them (§E D3).
- Interrupt-then-move needs no new semantics. Reload already replays only `_REPLAY_STATUSES` (`orchestrator.py:227`).

### C.2 Children, autopilot, PR Doctor, `hold:human`

- **Children are co-located by construction.**
  - `dispatch_child_session` creates the child in-daemon (`:23758-23790`).
  - `_notify_parent` (`:23840`) reads the parent from **this daemon's** `_records`, and so does `reply_to_child_session` (`:23907-23916`).
  - Moving one end of a live parent–child pair breaks child notices and steering. Refuse it, or move the tree (§E D5).
    Forks are exempt (`child_sessions.steering_parent`, `child_sessions.py:58-64`).
- **Autopilot** acts only on sessions `_owns_record` says are local, and nudges only idle sessions. Hold `_swapping`
  for the move, so a nudge waits for the target driver like any prompt during a rebuild.
- **PR Doctor and Backlog Runner** claims are keyed on session id (§A.3) and survive.
- **`hold:human`** is a `PreToolUse` hook built into every driver (`server/sessions/human_hold_guard.py`). The
  target's driver carries it.
- **Assistant sessions** are pinned to the primary (`fleet_orchestrator.py:428`): refuse.
- **Platform sessions** (`sessions.platform`, `models.py:357`, e.g. `windows`) may only target a host that advertises
  the platform (`fleet.platform_rejection` `:79`).

### C.3 Drain and close paths, and split-brain

- **Existing paths.**
  - `begin_drain` (`orchestrator.py:21675`) and `drain_on_signal` (`server/ipc/drain_on_signal.py`) are **host-wide**;
    neither drains one session.
  - The per-session teardown primitive is `_teardown_driver_state` (`:6538`), used by `close_session` (`:6775`, which
    also removes the worktree and marks the row `ended`, so **not** a move primitive) and `archive_session`.
  - The swap primitive is `_reassign_locked` (`:26824`). A move's source half is its close half, without the relaunch.
- **Split-brain guards that apply unchanged.**
  - Layer 1 (stamp) and the lease in `_claim_session`.
  - Layer 4 `_reconcile_foreign_lease`: if the source still drives the session after the restamp, its next lease tick
    closes it, except mid-turn.
  - `_ensure_session_cwd` refusing `foreign_host`.
- **Three gaps a move must close itself.**
  1. **The primary's in-memory stamp.** After a dobby → rocky move, the primary's `_records[sid].host` still says
     `dobby`.
     - The session is idle on rocky. Its lease is released whenever rocky has no driver (§A.1), and the source
       worktree still exists if it was kept (the parent spec's "kept until confirmed").
     - In that state any primary path that builds a driver (stuck sweep, rotation, a mis-routed `ensure_driver`)
       passes layer 1 and the lease, and **resumes the conversation in the stale dobby tree**. That is the 2026-09-07
       incident shape.
     - Fix: the source sets `rec.host = target` (primary) or pops it (worker) under `_swapping`, before the edge
       restamps the row.
  2. **The edge route cache** (§A.1). The saga runs in the edge's `FleetOrchestrator`, so it writes `_host_cache`
     directly. It should also drop and retry once on "no session with id" (the 2026-09-14 spec's self-heal).
  3. **Ordering.** Source closes and releases → edge restamps → target loads the row, sets `_records[sid].host = me`
     **before** registering the driver (layer 4 has no adopt bypass), and claims with `adopt=True`.
     - If the target fails, restamp back and re-import on the source (its worktree is still there).
     - Never restamp before the source has closed.

## D. Code map (file:line)

| Surface | Server | Web |
|---|---|---|
| Fleet routing | `fleet_orchestrator.py:202` (`_PER_SESSION`), `:295` (`_host_cache`), `:372` (`_host_for`), `:393` (`_make_per_session`), `:405` (`create_session`), `:492` (`_dispatch`), `:685` (`_stamp_host`), `:802` (`list_sessions`), `:1094` (`subscribe`), `:1100` (`__getattr__`); `fleet_router.py:80,157`; `fleet.py:79,86,103,119` | `api/fleet.ts:107` (`hostDispatchable`), `:158` (`useFleetHosts`), `:176` (`useEnabledFleetHosts`) |
| Ownership and lease | `session_lease.py:51-80`; `primary_lease.py:124`; `orchestrator.py:564` (`_stamp_is_this_host`), `:912` (TTL), `:3435` (`_claim_session`), `:3551` (release), `:3566` (tick), `:3675` (layer 4), `:25217,25237` (`_owns_record*`) | — |
| Rehydrate and record | `orchestrator.py:19602` (`_record_from_row`), `:19682` (`rehydrate`), `:19702` (worker partition), `:28623,28673` (mirror resync and evict) | — |
| Driver lifecycle | `orchestrator.py:6538` (`_teardown_driver_state`), `:6669` (`_ensure_session_cwd`), `:6775` (`close_session`), `:22383` (`reload_session`), `:22610` (`ensure_driver`), `:26824` (`_reassign_locked`), `:21675` (`begin_drain`), `:2720,2774` (automation execution) | — |
| Worktree | `src/viberpit/worktree_transfer.py:79,125,189,252,270`; `worktrees.py:410,478,939,1184,1293,1333`; `gitrepos.py:378` | — |
| Transcript | `orchestrator.py:24853` (`transcript_path`), `:24958` (`_find_transcript`); `configdir.py:32` | — |
| Children | `orchestrator.py:23758` (`dispatch_child_session`), `:23840` (`_notify_parent`); `child_sessions.py:58` | — |
| RPC | `server/ipc/orchestrator_rpc.py:332` (`_SIMPLE`; `reload_session` at `:362`), `:297,535`; `server/sessions/remote.py:901,917,937` (mirrors); `facade.rpc_deadline` | — |
| DB | `models.py:247` (`host`), `:95,101,110,113` (resume inputs), `:137,145,151` (background and pending), `:253` (`display_mode`), `:286` (`parent_session_id`), `:357` (`platform`), `:684-712` (`SessionRewind`), `:1534` (`DaemonLease`), `:1564` (`HostHeartbeat`) | — |
| API | `api/sessions.py:2758` (`/reload`, the pattern to copy), `:2798` (`/rotate`), `:2325` (`/fork`), `:1039` (create `host`), `:279` (`host` on the record), `:408` (`liveness`); `api/fleet.py:232` (`GET /hosts`) | `api/hooks.ts:716` (`useReloadSession`, the pattern) |
| UI | — | `features/sessions/SessionActions.tsx:28,43,205` (opt-in `onReload`/`onRotate` items: add `onMoveHost`); `SessionHeader.tsx:87-89,234` ("Runner host" identity row), `:259`; `MainPaneSession.tsx:128`; Runner select pattern `list/DispatchBar.tsx:182,598`; `settings/sections/FleetSection.tsx:129` |

In-flight work: no open PR touches `worktree_transfer`, `fleet_router`, `fleet_orchestrator`, `session_lease` or
`SessionActions`. The one match on "transfer" is #3313 (D-16c, plugin install fan-out). GraphQL check, 2026-11-05.

## E. Recommended design and owner decisions

### E.1 Minimal safe design

Adopt the 2026-09-14 transfer design's Phase 1 **manual move**, narrowed and with §6's corrections.

1. **Movable predicate** (pure function; the API, the menu and any later policy share it). All of:
   - Engine is Claude: a new `AgentCapabilities.cross_host_transfer` flag, false for Codex (`engine.py:43`).
   - Not an assistant.
   - `display_mode` is not native.
   - Not mid-turn, not inactive, not `queued` (queued sessions move by restamp alone).
   - No live steering children and no live steering parent.
   - A managed worktree whose cwd is under the **current** base, so the path stays identical.
   - The target is enabled, healthy, passes the engine and platform checks, has capacity, and is not the source.
2. **Claim.** `UPDATE sessions SET moving_to=:t WHERE id=:id AND moving_to IS NULL` (new nullable column).
   - Per-session RPCs that would build a driver answer 409 `transfer_in_flight`.
   - The bridge shows "Moving to rocky…" and re-attaches.
3. **Export on the source** (a new `transfer_export` RPC, routed explicitly):
   - inside `_swapping`, re-check idle;
   - `_teardown_driver_state` + `aclose`, then `_automation_driver_closed`;
   - `worktree_transfer.export` (WIP only with consent);
   - release the lease;
   - set or pop the source's in-memory record (§C.3 gap 1);
   - expire the latest rewind undo;
   - return the manifest, including pending preambles.
4. **Restamp** at the edge: `host`, `moved_from`, `moved_at` (`db_utc_now()`), plus `_host_cache[sid] = target` and an
   audit row `session.move`.
5. **Import on the target** (`transfer_import`):
   - load the row through `_record_from_row`, with `rec.host = me`;
   - `worktree_transfer.import_`;
   - `_claim_session(adopt=True)`;
   - `ensure_driver` (`resume=engine_session_id`);
   - queue a one-line "moved from dobby" preamble (`_queue_prompt_preamble`);
   - `delete_transfer_refs`.
   On failure: restamp back, import on the source (reuse path), and outcome `restored`.
6. **Tombstone the source.** It keeps no record (worker) or a mirror pointing at the target (primary), and keeps the
   worktree per D4.
7. **API and UI.**
   - `POST /api/sessions/{id}/move {host, carryDirty?}`, shaped like `/reload`.
   - "Move to host…" in `SessionActions` with a dialog in `SessionHeader`: a Runner select, a dirty-file line with a
     consent box, disabled mid-turn.
   - Kill switch `VIBERPIT_SESSION_MOVE_ENABLED`.
8. **Verification gate (Principle III).** Before code, run the 2026-09-14 spec's drill on an **archived** session:
   recreate its worktree on rocky and `--resume` it there. P1 and P2 already cover the CLI half without a model.

### E.2 Owner decisions

**D1. Uncommitted changes.**
- (a) **(Recommended)** **WIP commit to a hidden transfer ref, only on explicit consent** (the shipped
  `worktree_transfer.export`). Untracked files travel, and the ref is deleted after import.
  - The cost: uncommitted bytes, including any un-ignored secret file, reach GitHub. They remain fetchable by SHA until
    GitHub garbage-collects them.
- (b) Refuse a dirty tree; the user (or the agent) commits first. Nothing is exposed, and it is the simplest. It is
  friction on every real move, since an idle agent usually has edits.
- (c) `git bundle` relayed through the edge. Nothing reaches GitHub, and it would work without push rights. It needs a
  new streamed-bytes RPC both ways, a second transport the 2026-09-14 design rejected.

**D2. Codex sessions.**
- (a) **(Recommended)** **Not movable.** The capability flag is false and the menu shows "Codex history stays on its
  host". History lives in a host-local `CODEX_HOME`, and `thread/resume` has no path or history input.
- (b) Move as a **new thread** on the target with a ViberPit-written handoff summary (the `account_switch_new_thread`
  precedent). It works, but the native history and tool state are lost, and the session is then a different
  conversation under the same id.
- (c) Copy the rollout and state DB across. **This is ruled out**: it reads Codex private state (design D8,
  Principle I).

**D3. Running turns and background work.**
- (a) **(Recommended)** **Idle-only. `waiting_external` is allowed only after a confirm** that lists the background
  tasks that will stop; they are re-armed on the target through the existing rearm path. A running turn shows "Wait
  for the turn to finish or Interrupt first".
- (b) Idle-only, and refuse `waiting_external` outright. The simplest, but a session with a Monitor never becomes
  movable.
- (c) Interrupt-and-move with Reload's replay rule. One tap, but it risks re-running destructive work, and it adds
  prompt-replay semantics to a cross-host saga.

**D4. The source worktree after a move.**
- (a) **(Recommended)** **Keep it until the target's driver has started, then `finalize`**, after a
  `snapshot_recovery_ref` (local), so a bad move is recoverable on the source for 5 snapshots' worth of time. This
  matches the parent spec ("kept until the move is confirmed") and closes the stale-tree resume hazard (§C.3).
- (b) Keep it indefinitely (locked) for the janitor. Safest for the data, but it leaves a live-looking tree that a
  mis-stamped resume can pick up, and it costs disk.
- (c) Delete it right after export. Cleanest, but the restore-to-source path then has to refetch.

**D5. Parent and child sessions.**
- (a) **(Recommended)** **Refuse** when the session has live steering children or is a live steering child. Forks
  are exempt.
- (b) Move the whole live tree together: N worktrees and N resumes in one saga, with partial-failure handling.

**D6. Scope of D-17.**
- (a) **(Recommended)** **The manual per-session move only.** "Drain host" and the rebalancer (Phases 1b and 2 of the
  2026-09-14 spec) and dead-host adoption (#949) stay out. D-17 still extracts the shared pieces they need (the per-row
  import, the move predicate), so #949 can reuse them later.
- (b) Also ship "Drain host" in Settings › Fleet. It is useful for maintenance, but it is a batch saga with a
  per-session leave-behind report, which roughly doubles the API and UI.

### E.3 Should D-17 be split?

Yes, three PRs. The first two are needed for the benchmark row.

- **D-17a, server:**
  - the predicate and the capability flag;
  - `moving_to`, `moved_from` and `moved_at` (nullable, plus an alembic revision);
  - the per-row `transfer_import` load (one mapper, `_record_from_row`);
  - the `transfer_export`, `transfer_import` and `transfer_finalize` RPCs;
  - the edge saga with the restore path;
  - route-cache self-heal and `SESSION_MOVING`;
  - audit and the kill switch;
  - saga tests against two fake orchestrators (one per failure point, asserting the stamp, lease, `moving_to`, the
    in-memory host on both sides and the audit row).
- **D-17b, web:** the menu item, the dialog (Runner select, dirty consent, disabled mid-turn) and the card's "moving
  to …" chip, plus the full `npm run test`.
- **D-17c, a decision-gated follow-up:** "Drain host" (D6 b), and Codex-as-new-thread (D2 b) only if chosen.

## 6. Facts in the 2026-09-14 design that are now stale

- **Paths and line numbers.**
  - `src/ccswitch/…` is now `src/viberpit/…`.
  - Worktrees moved from `<repo_root>/.ccswitch/worktrees/…` to `<repo_root>/viberpit-sessions/worktrees/…` (#2934).
    Legacy trees remain on rocky, and `import_` targets only the new base.
  - Every `orchestrator.py`/`fleet_orchestrator.py` line reference has moved. Use §D.
- **Two gaps it does not mention.**
  - The primary's **in-memory** `rec.host` (§C.3 gap 1).
  - A worker's `ensure_driver` returning None for a row it has not rehydrated (§B.3).
- **`_ensure_session_cwd`** (#2913, after that spec) now refuses a missing managed worktree stamped for another host.
  The target must therefore `import_` the worktree **before** `ensure_driver`, and must hold the new stamp when it
  calls it.
- **Leases are released for idle detached sessions** (§A.1), so for an idle session the lease is no guard at all. The
  stamp and the in-memory host are what stand between a move and a twin resume.
- **The rocky binary differs** (2.1.293 against dobby's 2.1.318). P2 shows the older CLI resumes the newer
  transcript.

## Gaps (not verified)

- **GitHub push to the hidden namespace.** No `refs/viberpit/transfer/*` ref has ever been pushed to GitHub (tests use
  a local bare origin). Not checked:
  - acceptance under the per-repo GitHub App token (`git_env_for_remote`);
  - org rulesets (cairnea/KinDash);
  - whether deleted transfer objects stay fetchable by SHA.
- **A real cross-host resume** with a model turn, on rocky, of a session whose worktree was recreated by `import_`.
  The P1 and P2 probes ran on dobby only (the rocky binary was run on dobby; nothing executed on rocky).
- **CLI resume speed** when the cwd slug differs and the lookup has to scan ~2617 project dirs over sshfs. The probe
  config dir held 1–2 slugs.
- **Whether `--resume` writes new entries with the new `cwd`** and whether that affects later `transcript_path`
  fast-path stats. P1 showed the CLI appends to the old file; the per-entry `cwd` field was not inspected.
- **Background-task rearm on a different host** (`_rearm_session` `orchestrator.py:20038`). It is built for a restart
  on the same host.
- **Copilot** sessions on the target (proxy spin-up during resume). Not exercised.
- **`codex app-server`** `thread/fork` or other cross-thread primitives beyond `ThreadResumeParams`. Not surveyed.

## Gate results (2026-11-05)

Task 0 of the D-17 plan, run 11:20–11:26 EST against GitHub, dobby (CLI 2.1.318) and rocky (CLI 2.1.293).
Scratch clones and worktrees lived under `/tmp/d17-gate-38a5238a` on both hosts. Every git network call went through
the product's token path: `server.github.app_auth.register_git_providers()` then `gitrepos.git_env_for_remote(url)`,
with the daemon's App id and the `pr-dashy` key. Each call logged `app_token=yes`, so no call fell back to ambient
auth. No daemon, DB row, real session, real worktree or branch was touched. The gate made two model calls, both
with `--model haiku --max-turns 1` and a setup token (dobby: `crypto.read_oauth_token`; rocky: the runner's
broker envelope, as `oauth_env_for` does). Each ran in a throwaway `CLAUDE_CONFIG_DIR` under `/tmp` with no
`.credentials.json`, whose `projects` linked to the shared `~/.claude/projects`.

### Gate 1: GitHub accepts the hidden ref — PASS

- **Push (dobby, shallow scratch clone).** `git push --no-verify origin <sha>:refs/viberpit/transfer/d17-gate-38a5238a`
  returned `* [new reference] 17cc2db9… -> refs/viberpit/transfer/d17-gate-38a5238a` in 2.25 s. GitHub raised no
  refusal and no ruleset or protection message. `ls-remote` listed the ref.
- **Fetch (rocky, its own scratch clone, same token path).** It returned `* [new ref] …` in 1.56 s, and
  `log -1` showed the throwaway commit.
- **Delete.** `push origin :refs/…` returned `- [deleted]`. Afterwards `git ls-remote …/viberpit 'refs/viberpit/*'` was empty.
- **Design-relevant: deleted transfer commits stay public.**
  - After the delete, a fresh `git init` + `git fetch --depth 1 https://github.com/steiner385/viberpit 17cc2db9…`
    still succeeded (`cat-file -t` gave `commit`).
  - `https://github.com/steiner385/viberpit/commit/17cc2db9…` returned **HTTP 200**.
  - **`steiner385/viberpit` is a public repo** (`private=false`).
  - So a D1(a) WIP commit is **published**: anyone who has the SHA can read it, with no auth, until GitHub GCs it.
    Deleting the ref does not retract it.
- **Not checked.** Org rulesets on `cairnea/KinDash`, because the rules allowed only `steiner385/viberpit`.

### Gate 2: cross-host resume of a session whose worktree came over by `import_` — PASS

- **Setup on dobby.**
  - The worktree was at `/tmp/d17-gate-38a5238a/GitHub/viberpit-sessions/worktrees/viberpit/d17`
    (`worktrees_base(repo_root)/viberpit/_short("d17-gate-1e459d18")`, the product layout with
    `repo_root=/tmp/d17-gate-38a5238a/GitHub`). It was on local branch `d17-gate/1e459d18`, with one untracked file.
  - Call 1: `claude --session-id 0f4e80b2-… -p "Remember this codeword for later: PERSIMMON-38a5238a. Reply with just OK."`
    returned `OK` (exit 0, 1.49 s).
  - The transcript was written under the worktree slug: 26 lines, every `cwd` the worktree, `version` 2.1.318.
- **Export (dobby).** `worktree_transfer.export(dest, "d17-gate-1e459d18", carry_dirty=True)` took 2.36 s.
  - It wrote the WIP commit `42381669`, base `ef0b8e56`, and pushed `refs/viberpit/transfer/d17-gate-1e459d18/head`.
  - The current branch is correctly not pushed as a branch (`branches={}`).
- **Import (rocky).** `worktree_transfer.import_(bundle, <scratch clone>, "/tmp/d17-gate-38a5238a/GitHub")` took 2.07 s.
  - It recreated the worktree at the **byte-identical path** on branch `d17-gate/1e459d18`, with HEAD at the base `ef0b8e5`.
  - The scratch file arrived as untracked (`??`) with its content intact, and the worktree was locked
    (`ViberPit live session`).
- **Resume (rocky, CLI 2.1.293).** Call 2: `claude --resume 0f4e80b2-… -p "What was the codeword I asked you to
  remember? Reply with just the codeword."` was run from the imported worktree.
  - **Exit 0. Output `PERSIMMON-38a5238a`**, so the history loaded.
  - Wall time was 4.94 s, of which ~3 s was the CLI's "no stdin data received in 3s" wait (`ssh` without `-n`).
    The no-model run below puts lookup-to-first-request at 0.89 s.
- **Transcript after resume.**
  - It grew 26 → 36 lines (231491 → 238557 B) **in the original file**, and no new file appeared in that slug.
  - The 10 appended entries carry `cwd` = the same worktree path (5 entries), `version` 2.1.293, and the same `sessionId`.
  - Because the path is identical, the `cwd` is unchanged and `transcript_path`'s fast path stays valid.
- **Side effect.** The CLI also created an empty `projects/<slug(clone path)>/memory/` dir, keyed on the git
  common-dir parent. Auto-memory therefore follows the **clone** path, which is also identical on both hosts
  (`~/GitHub/.ccswitch/clones/<owner>/<name>`).

### Gate 3: resume lookup speed over sshfs (rocky, real `projects/`, 2624 dirs) — PASS, with a caveat

No model ran in this gate: a dummy API key, and `ANTHROPIC_BASE_URL` pointed at a local stub that answers 400.

| Probe on rocky | Time |
|---|---|
| `readdir projects/` | 0.03 s |
| `glob projects/*/<id>.jsonl` (the `_find_transcript` fallback), hit / miss | 5.02 s / 5.19 s (dobby, local disk: 0.08 s) |
| CLI `--resume <id>`, **same cwd** (slug matches), to first API request | **0.89 s** |
| CLI `--resume <id>`, **other cwd** (slug differs), to first API request | 7.16 s; a second run took 8.26 s |
| CLI `--resume <bogus id>` → `No conversation found` | 8.83 s |

- A slug-mismatched resume costs ~7–8 s, not the 200 s seen in 2026-08. **Keeping `cwd` identical is still worth
  ~7 s per move** and keeps ViberPit's own lookup on the fast path.
- The other-cwd resume also created an empty `projects/<slug(other cwd)>/memory/`. It appended to the original
  file, as P1 found.

### New finding: rocky's sshfs attribute cache can hide the source's last turn (no model, stub only)

- **The mount.** rocky mounts `~/.claude` with `dir_cache=yes,dcache_timeout=600,attr_timeout=600,entry_timeout=600`
  (`ps` on rocky).
- **Probe.**
  1. Write a synthetic two-entry transcript on dobby (exchange "PAPAYA").
  2. On rocky, `stat` and `cat` it (977 B cached).
  3. On dobby, append a second exchange ("MANGO"), giving 1986 B.
  4. rocky's `stat` still said 977.
- **Result.** The rocky CLI `--resume` sent **3 messages containing only PAPAYA**. The control (both exchanges
  written before rocky ever looked) sent **5 messages, PAPAYA and MANGO**.
  - The CLI's own append landed at the true EOF; no file was corrupted (30 or 37 lines, 0 bad).
  - But the resumed conversation would branch off the stale leaf, so **the source's last turn is lost from the
    model's context**.
- **Remedy (measured).** Reading the file to EOF on rocky refreshes the cached size (stale 8 → 16 after
  `open().read()`). An `O_APPEND` open and close does not.
- **Scope.** This affects only a move **into** an sshfs host (dobby → rocky) when the target has stat'ed or
  listed that transcript in the last 600 s. Any readdir of the slug dir caches attributes. rocky → dobby reads
  dobby's local disk.

### Design changes

1. **`transfer_import` must refresh, then verify, the transcript before `ensure_driver`.**
   - The source's `transfer_export` manifest carries the transcript's byte size and last entry `uuid`, read after
     the driver closes.
   - The target reads the file to EOF (this refreshes sshfs), then checks the size and the last `uuid`. If they
     don't match, it retries briefly, then fails the move with outcome `restored`, rather than resuming a truncated
     history.
   - Add this to §E.1 step 5 and to the D-17a saga tests.
2. **D1(a) cost: the WIP is published, not merely "exposed".**
   - On a public repo the WIP commit is world-readable by SHA after ref deletion, verified above.
   - The consent copy must say so.
   - Either default to D1(b) (refuse a dirty tree) for **public** repos, or require that the consent names the repo
     as public.
   - This bears on owner decision D1. It does not block Task 1.
3. **No change needed.**
   - The hidden namespace is accepted under the App token, and so is cross-host fetch.
   - `import_` keeps the path identical, so `cwd`, the slug, `transcript_path`'s fast path and the auto-memory slug
     all match.
   - 2.1.293 resumes and appends to a 2.1.318 transcript.

### Cleanup (verified)

- **GitHub.**
  - Gate 1's ref was deleted by explicit push.
  - Gate 2's `d17-gate-1e459d18/head` was deleted with `worktree_transfer.delete_transfer_refs`.
  - Final `git ls-remote https://github.com/steiner385/viberpit 'refs/viberpit/*'` printed 0 lines.
  - The two commits stay fetchable by SHA (see above).
- **dobby.** These were removed:
  - `/tmp/d17-gate-38a5238a` (clone, worktree, config dir) and the scripts dirs;
  - every `~/.claude/projects/*d17-gate*` slug dir. A final `ls` matched none.
- **rocky.** `/tmp/d17-gate-*` was removed; a final `ls` matched none. Its sshfs view of the deleted slug dirs stays
  cached for up to 600 s, which is the same caching as above.
