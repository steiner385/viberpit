# Host Platforms (Windows VM routing) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a runner advertise extra platforms (rocky: `windows`, via its KVM VM). The Backlog Runner
then dispatches `env: windows` issues only to that host, one working session at a time, and the
session is told how to drive the VM desktop.

**Architecture:** Mirror the engine-advertisement path: daemon `/info` → edge probe → `Host` row →
`HostCandidate` → `rank_hosts`/`pick` hard filter. The queue's environment gate takes the live
platform union instead of a constant. The gated platform rides the dispatch chain to `pick` and is
stamped on `DbSession.platform`. The daemon appends a per-platform guide to session instructions.
A host-side `winrun` lock is the hard one-at-a-time guarantee.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy + Alembic, pydantic-settings, pytest.

**Spec:** `docs/superpowers/specs/2026-11-03-host-platforms-windows-vm-design.md`. Read its
"Calls made by the designer" section; each task below cites the call it implements.

## Global Constraints

- New settings use the `VIBERPIT_` prefix and go in BOTH `Settings` classes in `server/config.py`
  (pydantic `:102` and fallback `:470`).
- New DB columns are nullable. Migration `0097_host_platforms.py` has `down_revision` =
  `0096_session_views`'s revision id.
- A daemon that omits `platforms` means `{"linux"}`. Apply that default in exactly one function,
  `parse_platforms_info`.
- Platform names are the OS keys of `_ENV_PLATFORMS` in `server/pm/queue.py`: `linux`, `windows`,
  `macos`. `linux` is always advertised.
- Never `func.now()`; never hand-rolled `task.cancel()`. See core.md.
- Run the full `lint-and-test` gate (ruff, mypy, `pytest tests/`, `pytest server/tests/`) before
  pushing, with `env -u VIBERPIT_SESSIOND_* -u VIBERPIT_DAEMON_ROLE` (and the CCSWITCH_ twins).

## Review Focus

1. **A Windows host goes unhealthy or drops `windows` while a gated session is queued for
   capacity.** Expected: the queued session never drains onto a Linux host. Test in Task 4:
   `test_capacity_queue_never_drains_gated_session_to_linux`.
2. **An `env: windows` issue on a product that pins engine `codex`.** Expected: refused with a
   clear reason, never spawned as Codex. Test in Task 4: `test_gated_dispatch_refuses_codex_pinned_product`.
3. **`env: windows` plus `env: linux` on one issue (a union).** Expected: not gated, so it runs
   anywhere, matching today's union semantics. Test in Task 3: `test_gated_platform_union_with_linux_is_ungated`.
4. **`VIBERPIT_HOST_PLATFORMS` with junk (`windows, ,WINDOWS,beos`).** Expected: normalized
   to `{"linux","windows"}`; unknown names are dropped with one warning, never crashing the daemon.
   Test in Task 1: `test_configured_platforms_normalizes_and_drops_unknown`.
5. **`winrun` while the VM is off.** Expected: a non-zero exit within the ssh connect timeout and a
   one-line message, without ever hanging the session. Test in Task 6: `test_winrun_unreachable_vm_fails_fast`.

---

### Task 1: Platform advertisement on the daemon (`/info.platforms`)

**Files:**
- Create: `server/sessions/host_platforms.py`
- Modify: `server/config.py` (both Settings classes), `server/ipc/daemon.py:~455-470` (the `/info` handler)
- Test: `server/tests/test_host_platforms.py`

**Interfaces:**
- Produces:
  - `PLATFORMS_INFO_KEY: Final = "platforms"`
  - `KNOWN_PLATFORMS: frozenset[str] = frozenset({"linux","windows","macos"})`
  - `configured_platforms(raw: str | None) -> frozenset[str]`
  - `class PlatformProbe` with `__init__(self, *, check: Callable[[str], Awaitable[bool]], ttl_s: float = 60.0, clock=time.monotonic)` and `async def advertised(self, configured: frozenset[str]) -> frozenset[str]`
  - `build_platforms_info(advertised: frozenset[str]) -> list[str]` (sorted)
  - `parse_platforms_info(value: object) -> frozenset[str]` (None, non-list or empty → `{"linux"}`; always includes `linux`; unknown names dropped)
  - `platform_dispatchable(platforms: frozenset[str], platform: str | None) -> bool`
  - Settings: `host_platforms: str = ""` (alias `VIBERPIT_HOST_PLATFORMS`) and `windows_vm_ssh_host: str = "win11"` (alias `VIBERPIT_WINDOWS_VM_SSH_HOST`)

- [ ] **Step 1: Write failing tests**
  - `test_configured_platforms_normalizes_and_drops_unknown`: `configured_platforms("windows, ,WINDOWS,beos") == {"linux","windows"}`; `configured_platforms(None) == {"linux"}`.
  - `test_parse_platforms_info_legacy_absent_is_linux`: `parse_platforms_info(None) == {"linux"}`, `parse_platforms_info("x") == {"linux"}`, `parse_platforms_info(["windows"]) == {"linux","windows"}`.
  - `test_build_parse_round_trip`.
  - `test_probe_drops_windows_when_check_fails`: fake check returns False → `advertised({"linux","windows"}) == {"linux"}`.
  - `test_probe_caches_for_ttl`: check called once across two calls within ttl, twice after the fake clock advances 61 s.
  - `test_probe_never_checks_linux_only`: configured `{"linux"}` → check never called.
  - `test_info_payload_carries_platforms`: through the daemon `/info` handler test seam used by the existing engines `/info` test (`grep -rn ENGINES_INFO_KEY server/tests`).
- [ ] **Step 2: Run** `pytest server/tests/test_host_platforms.py -v`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement**
  - The probe's real check runs `ssh -o BatchMode=yes -o ConnectTimeout=5 <windows_vm_ssh_host> exit` via `asyncio.create_subprocess_exec` with a 10 s overall timeout. Any error counts as False.
  - The check runs only for the non-Linux platforms that are configured (only `windows` maps to the VM check; `macos` passes through unchecked).
  - Build one `PlatformProbe` per daemon process, so the cache persists across requests.
  - Log a warning once per unknown configured name.
- [ ] **Step 4: Run** the tests. Expected: PASS. Also `pytest server/tests -k "info and engines" -q`. Expected: still PASS.
- [ ] **Step 5: Commit** `feat(fleet): runners advertise platforms on /info`

### Task 2: Edge storage and the host filter

**Files:**
- Create: `server/db/migrations/versions/0097_host_platforms.py`
- Modify:
  - `server/db/models.py`: `Host.platforms` JSON nullable, next to `engines` at `:1473`; `DbSession.platform` String(16) nullable.
  - `server/sessions/host_probe.py:109-114` and a `_platforms_snapshot(info)` beside `_engines_snapshot`.
  - `server/sessions/fleet.py`: `HostCandidate.platforms`, `platform_rejection`, `rank_hosts(platform=)`.
  - `server/sessions/fleet_router.py:131-205`: `candidates()` fills platforms; `pick(platform=)`.
  - `server/api/fleet.py`: each host's payload gains `platforms: list[str]`.
  - The Postgres lists in `server/tests/test_postgres_compat.py` / `test_schema_reconcile_coverage.py`, only if a test demands it (nullable columns normally need nothing).
- Test: `server/tests/test_fleet_platforms.py`

**Interfaces:**
- Consumes: `parse_platforms_info`, `PLATFORMS_INFO_KEY` (Task 1).
- Produces:
  - `HostCandidate.platforms: frozenset[str] = frozenset({"linux"})`
  - `platform_rejection(c: HostCandidate, platform: str | None) -> str | None`
  - `rank_hosts(..., platform: str | None = None)`
  - `FleetRouter.pick(..., platform: str | None = None)`
  - `DbSession.platform: Mapped[str | None]`

- [ ] **Step 1: Write failing tests**
  - `test_probe_persists_platforms_including_fenced_outcome`
  - `test_rank_hosts_platform_filters_before_scoring`: a higher-scored linux-only host is excluded for `platform="windows"`.
  - `test_rank_hosts_platform_none_unchanged`
  - `test_pick_platform_ignores_prefer_of_ineligible_host`: prefer=dobby, platform=windows → `["rocky"]`.
  - `test_pick_no_platform_host_raises_no_host_available`
  - `test_fleet_api_lists_platforms`
  - `test_legacy_host_row_null_platforms_is_linux`
- [ ] **Step 2: Run** `pytest server/tests/test_fleet_platforms.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement**
  - Platform filtering goes beside the engine filter in `rank_hosts`.
  - In `pick`, a platform-ineligible `prefer` is dropped, the same way the engine path does it.
  - When no host qualifies, raise the existing `NoHostAvailable`, with the platform named in the message.
  - The migration adds both columns.
- [ ] **Step 4: Run** the new tests plus `pytest server/tests -k "fleet or host_probe or schema" -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(fleet): persist host platforms and filter dispatch by platform`

### Task 3: Queue gate (live platforms and `platform_busy`)

**Files:**
- Modify:
  - `server/pm/queue.py`: `gated_platform`; `platforms`/`busy_platforms` keywords on `skip_reason` (`:376`) and `rank` (`:497`); the `SKIP_REASONS` entry; the `FLEET_PLATFORMS` comment.
  - `server/pm/policy_skip.py:66` and `server/pm/escalation_approval.py:78`: thread `platforms`.
  - `server/automations/product_policy.py:786` `rank_kwargs` stays policy-only. Add `async def fleet_platform_state(db) -> tuple[frozenset[str], frozenset[str]]` in a new `server/pm/fleet_platforms.py`.
  - Callers: `server/sessions/orchestrator.py:~8420`, `server/api/pm.py:1055`, `server/api/pm_tree.py:77`, `server/pm/escalation.py:200,262`, `server/pm/escalation_approval.py:70`.
  - The assistant tool's skip-reason description (find it with `grep -rn '"needs_spec"' server/assistant`) and any web skip-reason label map (`grep -rn needs_spec web/src`).
- Test: `server/tests/test_pm_queue.py` (extend), `server/tests/test_fleet_platforms_state.py`

**Interfaces:**
- Consumes: `Host.platforms`, `Host.enabled`, `Host.healthy`, `DbSession.platform` and `DbSession.status` (Task 2); `parse_platforms_info` (Task 1); `ACTIVE_SESSION_STATUSES` from `server/pm/desired_state.py`.
- Produces:
  - `gated_platform(labels: Iterable[str]) -> str | None`: returns `"windows"`/`"macos"` only when every OS-naming `env:` label excludes linux; `both`, `any`, `real-target` and unlabelled give None. There is ONE platform per gated issue; if several non-Linux platforms are named, pick the first in sorted order.
  - `skip_reason(..., platforms: frozenset[str] = FLEET_PLATFORMS, busy_platforms: frozenset[str] = frozenset())`, and the same on `rank`.
  - `fleet_platform_state(db) -> (advertised_union, busy)`. `busy` is a non-Linux platform for which every enabled, healthy advertiser hosts ≥1 `DbSession` with `platform == p` and `status in ACTIVE_SESSION_STATUSES` (design call 1).
  - `"platform_busy"` is appended to `SKIP_REASONS`.

- [ ] **Step 1: Write failing tests**
  - `test_env_windows_runs_when_fleet_advertises_windows`
  - `test_env_windows_still_environment_skip_without_windows_host` (the #2983 regression)
  - `test_real_target_still_skipped_with_windows_host`
  - `test_gated_platform_both_is_none`
  - `test_gated_platform_union_with_linux_is_ungated`
  - `test_platform_busy_after_environment`: no advertiser gives `environment`, not `platform_busy`.
  - `test_platform_busy_when_only_advertiser_has_working_session`
  - `test_handed_back_session_does_not_hold_platform`
  - `test_fleet_platform_state_ignores_unhealthy_and_disabled_hosts`
- [ ] **Step 2: Run** `pytest server/tests/test_pm_queue.py server/tests/test_fleet_platforms_state.py -v`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement**
  - Keep `queue.py` pure; only the async callers query.
  - In `skip_reason`, the `platform_busy` check goes immediately after the `environment` check.
- [ ] **Step 4: Run** `pytest server/tests -k "pm_queue or policy_skip or escalation or assistant_tools or pm_tree" -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(pm): env-gated issues run when a host advertises the platform; platform_busy skip`

### Task 4: Routed dispatch

**Files:**
- Modify:
  - `server/automations/backlog_selection.py:15` (`Batch.platform`; a gated primary forms a singleton batch, and gated issues are never bundled into others).
  - `server/pm/queue.py` `to_candidate`/`QueueEntry` (carry `platform`).
  - `server/sessions/orchestrator.py` `_backlog_spawn` (`:9709`) and `_automation_create_session` (`:4180`).
  - `server/automations/context.py` `spawn_or_join_session` (~`:848`).
  - `server/sessions/internal_dispatch.py:37`.
  - `server/api/internal_sessions.py:57-91` (`platform: str | None = None`).
  - `server/sessions/fleet_orchestrator.py:391-520` (pass `platform` to every `pick`, including `_queue_hosts`).
  - `SessionOrchestrator.create_session` (`orchestrator.py:3896`): accept `platform` and stamp `DbSession.platform`.
  - The fork, child and respawn paths that copy session fields: inherit `platform` (design call 7). Find them with `grep -n "permission_mode=.*source\|\.permission_mode = " server/sessions/orchestrator.py`.
- Test: `server/tests/test_platform_dispatch.py`

**Interfaces:**
- Consumes: `gated_platform` (Task 3), `pick(platform=)` (Task 2).
- Produces:
  - `Batch.platform: str | None = None`
  - The `create_session(..., platform: str | None = None)` keyword through the facade, internal API and orchestrator.
  - A typed refusal `PlatformDispatchRefused(reason: str)`, raised before any spawn when:
    - (a) `platform` is set and automation host routing is disabled, or
    - (b) the product engine resolves to codex (design call 5).
    
    The runner records it as a refused dispatch, the way other spawn refusals are recorded.
  - With `platform` set, the engine is forced to `claude` when the product engine is `auto`/unset.

- [ ] **Step 1: Write failing tests**
  - `test_gated_issue_is_singleton_batch`
  - `test_dispatch_lands_only_on_platform_host`
  - `test_capacity_queue_never_drains_gated_session_to_linux`
  - `test_gated_dispatch_refuses_when_host_routing_disabled`
  - `test_gated_dispatch_refuses_codex_pinned_product`
  - `test_gated_dispatch_forces_claude_engine`
  - `test_session_row_stamped_with_platform`
  - `test_fork_and_respawn_inherit_platform`
  - `test_internal_create_rejects_unknown_platform` (422 for `"beos"`).
- [ ] **Step 2: Run** `pytest server/tests/test_platform_dispatch.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement.** Update BOTH session-facade test doubles if a new orchestrator call is added (`FakeOrch` and `_FakeSessions`; see the memory note on two test doubles).
- [ ] **Step 4: Run** `pytest server/tests -k "backlog or fleet_orchestrator or internal_sessions or automation" -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(automations): route env-gated issues to platform hosts only`

### Task 5: Per-platform session guide

**Files:**
- Modify:
  - `server/config.py`: `host_platform_guide_windows: str | None = None` (alias `VIBERPIT_HOST_PLATFORM_GUIDE_WINDOWS`), in both classes.
  - `server/sessions/tools/instruction.py:119` (`session_instructions(..., platform_guide: str | None = None)`, appended last).
  - `server/sessions/claude_sdk_factory.py:15` (`_build_append_string(platform_guide=None)`) and its caller in `_session_factory` (`orchestrator.py:~2424`), which reads the session's `platform`.
  - `server/codex/driver.py:641`: pass None; Codex never gets platform work (design call 5).
- Create: `docs/runbooks/windows-vm-session-guide.md` (the guide template installed on rocky).
- Test: `server/tests/test_platform_guide.py`

**Interfaces:**
- Consumes: `DbSession.platform` (Task 2).
- Produces: `load_platform_guide(platform: str | None, settings) -> str | None`. It reads the file each spawn, returns None for linux/None/an unset setting/a missing file, logs at INFO (not WARNING) on a missing file, and caps the text at 8 KiB.

- [ ] **Step 1: Write failing tests**
  - `test_guide_appended_only_for_windows_session`
  - `test_linux_session_on_same_host_gets_no_guide`
  - `test_missing_guide_file_is_not_an_error`
  - `test_guide_reapplied_on_respawn`
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement.** The guide template covers:
  - the VM exists at `ssh win11`;
  - always `winsync` the worktree, then `winrun <cmd>`;
  - `winrun` serializes desktop use;
  - leave the desktop clean (close windows you opened);
  - never read `~/.config/viberpit/win11-vm.secret` or the ssh key;
  - Windows CI on the PR remains the authority.
- [ ] **Step 4: Run** `pytest server/tests -k "instruction or session_tools or platform_guide or sdk_factory" -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(sessions): per-platform session guide for platform-routed sessions`

### Task 6: `winsync` / `winrun` host helpers and the runbook

**Files:**
- Create:
  - `scripts/winvm/winrun.py` and `scripts/winvm/winsync.py` (stdlib only, run with `python3`).
  - `docs/runbooks/windows-vm-on-a-session-runner.md`: provision (virt-install flags, autounattend, MSDM pass-through, setup.ps1), install of the helpers and guide, `VIBERPIT_HOST_PLATFORMS`, snapshot `clean` / restore, and a licensing note.
  - The autounattend/setup templates under `docs/runbooks/windows-vm/`, with secrets as placeholders.
- Test: `server/tests/test_winvm_helpers.py` (imports the scripts by path; fakes `ssh` via a PATH shim).

**Interfaces:**
- `winrun [--timeout S] [--cwd WINPATH] -- <command...>`:
  - Takes `fcntl.flock` on `~/.cache/viberpit/winvm.lock`, waiting at most `--lock-wait` (default 900 s), then exits 75 with `winrun: desktop busy`.
  - Over ssh, registers a one-shot interactive scheduled task as `viber` (`/IT`) that runs the command in the logged-on desktop, redirects stdout/stderr to `C:\winrun\<id>\out|err` and writes the exit code to `rc`.
  - Polls for `rc`, streams the output back, deletes the task, and exits with the remote rc.
  - Fails fast on an unreachable VM with `ConnectTimeout=5`.
- `winsync [--dest C:\work\<repo>] [path]`: mirrors the given worktree (default cwd), excluding `.git` contents except `HEAD` and refs and excluding ignored files, via `git ls-files -co --exclude-standard` + tar over ssh. Prints the Windows path.

- [ ] **Step 1: Write failing tests**
  - `test_winrun_returns_remote_exit_code`
  - `test_winrun_unreachable_vm_fails_fast`
  - `test_winrun_lock_serializes_and_times_out`: holds the lock in-test with a 1 s `--lock-wait` and expects exit 75.
  - `test_winrun_never_echoes_secret_paths_in_output`
  - `test_winsync_sends_only_tracked_and_untracked_unignored_files`
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** the helpers and write the runbook.
- [ ] **Step 4: Run** `pytest server/tests/test_winvm_helpers.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(winvm): winrun/winsync desktop helpers and the Windows VM runbook`

### Task 7: Real end-to-end on rocky (controller, not a subagent)

- [ ] Install the helpers onto rocky's PATH and the guide at `/etc/viberpit/windows-guide.md`. Add `Host win11` to `~/.ssh/config`. In VM: Python 3.12 + Git for Windows (`winget`, or silent installers over ssh). Take a libvirt snapshot `clean`.
- [ ] `winrun -- python -c "import ctypes;print(ctypes.windll.user32.GetForegroundWindow()!=0)"` prints `True`, proving it runs in the interactive desktop.
- [ ] Clone ghostdriver-mcp into the VM via `winsync`. `winrun -- python -m pytest` passes the same way as Windows CI.
- [ ] After merge and rollout: set `VIBERPIT_HOST_PLATFORMS=linux,windows` and the guide path in rocky's daemon env. Restart only when nothing is mid-turn.
- [ ] `GET /api/fleet` shows rocky's `platforms` including `windows`. `GET /api/pm/queue` for ghostdriver-mcp shows #248 / #257 no longer skipped for `environment`, and one of them shows `platform_busy` while the other runs.
- [ ] Stop the VM: within about 60 s `windows` disappears from rocky's platforms, and the issues revert to `environment`.
