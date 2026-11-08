# Desktop-parity D-23: opt-in, fail-closed sandbox for Claude sessions — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal.** Add the Claude CLI's built-in sandbox (bubblewrap + socat on Linux) as an opt-in, fail-closed, per-session toggle.

**Decisions.**
- D22a: adopt, opt-in, fail-closed.
- D22d (2026-11-06) adopts all five spike amendments. Both are in `docs/audits/2026-11-01-claude-desktop-code-benchmark.md` §7. The amendments:
  1. No unix sockets. gh uses a file-backed `GH_CONFIG_DIR`.
  2. In full-auto the sandbox is containment only.
  3. socat installed on rocky.
  4. Turning the toggle on respawns the session.
  5. A strict GitHub-only network allowlist.

**Source of truth for facts:** the spike, `docs/superpowers/specs/2026-11-06-d23-sandbox-spike.md`.
- §1: findings, with binary offsets for CLI 2.1.319 and 2.1.293.
- §2: recommended design.
- §3: risks.

Read it first. This plan only adds sequencing and constraints.

## Global Constraints

**Kill switch (Principle I).** `VIBERPIT_SANDBOX_ENABLED`, default **on** for the capability. The feature is still off per session by default. With the switch off, the toggle is hidden and no sandbox settings are ever sent.

**Claude only.**
- Add a capability flag on the engine (`AgentCapabilities`).
- Codex is unchanged.

**Settings.**
- The `AppSettings.sandbox_default` column is nullable; null means off.
- The per-session column `sessions.sandbox` is nullable. Null means "follow the default at spawn". Record the effective value at spawn.
- Add a migration chained off the newest one, plus the new-model and column checklist from `core.md`.

**Spawn.**
- When the effective value is on, merge spike §2.1's exact `sandbox` block into the existing `spawn_settings()` / `--settings` JSON.
- Always send the **full** block. Merge semantics are unverified (risk 11).
- Set `GH_CONFIG_DIR` in the session env to the host's file-backed gh config when one is configured. On dobby that is the existing `VIBERPIT_GH_CONFIG_DIR`. Also set `GH_NO_UPDATE_NOTIFIER=1`.
- The domain list is a module constant: `github.com`, `*.github.com`, `*.githubusercontent.com`.

**Host preflight (spike §2.3).**
- The daemon runs it, cached per host, re-run at daemon start and on demand.
- It is exposed through the existing host and Environment facts path (reuse it; no parallel call).
- Its argv-only subprocesses each have a timeout:
  - `bwrap`/`socat` presence;
  - the `bwrap --ro-bind / / --dev /dev --unshare-net true` probe, with a 5 s timeout;
  - gh file-backed readiness;
  - CLI version ≥ 2.1.293.
- The first failure reason is what the toggle shows.

**After-spawn check (spike §2.4).**
- Send the `get_sandbox_dialog` control request and require every listed condition.
- Store the result on the session.
- If it fails, stop the turn and **hold** the session with the reason. The user can then choose "Retry" or "Turn off sandbox and continue", which is an explicit human action.
- Never auto-retry unsandboxed.
- The control request is `@internal`, so wrap it with a clear error if it is absent, and treat an absent request as **failed**.

**Startup failure.**
- Startup fails when `startup_failure_reason == "sandbox_required_unavailable"` or the output contains `❌ Sandbox Error`.
- The session then ends failed with that message.
- Log expected refusals at INFO. An unexpected failure to verify logs WARNING with a bare event name.

**Toggle.**
- `POST /api/sessions/{id}/sandbox {enabled}` needs a person (`require_human`) and writes an audit row `session.sandbox`.
- It is idle-only.
- ON respawns through the existing resume path with the new settings. OFF may use a live `apply_flag_settings {"sandbox":{"enabled":false}}` (verified safe, probe S) or respawn; choose the simpler path. Either way, re-check the state afterwards.
- It is a per-session RPC on the owning daemon (`_PER_SESSION`, `_SIMPLE`, remote, facade, fleet).
- The toggle is refused while a move is in progress.

**Web.**
- Environment card:
  - Sandbox state: `off`, `active`, or `failed: <reason>`.
  - The toggle, disabled with the preflight reason when unavailable.
  - A one-line note when on: "Network: GitHub only. Writes: this worktree only. In full-auto there are no prompts — the sandbox only contains commands."
- Settings › Sessions gets a "Sandbox new sessions" default toggle, shown only if the capability is on.
- The hold banner offers Retry and "Turn off sandbox and continue".

**Budget.** Keep new UI lazy where it can be. Ceilings are master's current values; measure + 0.3 only if the bundle moved.

**Gates.**
- Server: ruff, mypy, focused tests plus parity, the Postgres suite (new columns), and the full `server/tests -n 4`.
- Web: typecheck, lint, the full vitest suite, build, budget, e2e.

**Live probe (risk 9).** This is the one real-model run, in Task 3.

## Review Focus

1. **Fail-closed everywhere.** No path runs a session that was asked to be sandboxed while it is unsandboxed. That covers spawn failure, a failed check, a missing control request, a respawn, a resume, and a move.
2. **Turning on always respawns.** There is no live enable.
3. **The full settings block is always sent.** `allowUnsandboxedCommands:false`, `strictAllowlist:true` and `allowAllUnixSockets:false` are fixed in code, never configurable by a session.
4. **The toggle needs a person and is audited.** A token caller can't flip it.
5. **The kill switch removes everything.**

---

### Task 1: Server — preflight, settings, spawn, after-spawn check, toggle

**Files:**
- `server/sessions/sandbox.py` (new: constants, settings block, preflight, the dialog check)
- `claude_driver.py` (spawn settings and env, the check)
- orchestrator (spawn, respawn toggle, hold)
- models and migration
- `api/sessions.py` and `api/settings.py`
- the RPC, remote, facade and fleet layers
- engine capability

**Tests:** `server/tests/test_sandbox.py`, with fake CLI frames and fake subprocesses. Cover Review Focus 1–5.

- [ ] Write failing tests.
- [ ] Implement.
- [ ] Run gates.
- [ ] Commit `feat(sessions): opt-in fail-closed sandbox for Claude sessions (D-23)`.

### Task 2: Web — Environment card, Settings default, hold banner

**Tests:** unit tests and e2e `web/e2e/sandbox.spec.ts`, route-mocked at 1440 and 390.

- [ ] Write failing tests.
- [ ] Implement.
- [ ] Run gates.
- [ ] Commit `feat(web): sandbox toggle, state and hold actions (D-23)`.

### Task 3: Live verification and docs

**Probe.** On dobby, run one real sandboxed session through the CLI with the exact settings block. Run it outside ViberPit's live daemon: a scratch `CLAUDE_CONFIG_DIR` copy of one account and a throwaway clone. Record the results in the spike doc §5. The probe must verify:
- `git fetch`, plus `git push origin HEAD:<scratch-branch>` to a scratch branch on a scratch repo, and `gh pr view`;
- that a non-GitHub domain is denied;
- that a write outside the worktree is denied;
- the `get_sandbox_dialog` output.

Delete the scratch branch afterwards. Then run the same `get_sandbox_dialog` read on rocky.

**Docs.**
- Benchmark: "Sandbox settings" becomes Y. Regenerate §3.
- Update the spec row, the product docs, and `deploy/README.md` (sandbox host requirements: bwrap, socat, and a file-backed gh config).

- [ ] Commit `docs: D-23 sandbox verification and docs`.
