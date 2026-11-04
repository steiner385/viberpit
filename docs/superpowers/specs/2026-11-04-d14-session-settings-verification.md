# D-14 "Session settings": verification spike

Date: 2026-11-04. Constitution III: every claim below was checked in one of three ways: against the installed binary, by
a no-model probe, or in ViberPit's code. Claims that were not checked are listed under **Gaps**.
Parent spec: `2026-11-01-ui-desktop-parity-design.md` lines 44, 65, 112, 117, 338, 351-355, 378 and 412 (decisions
`[A-D14]`, `[A-D16]`; benchmark `docs/audits/2026-11-01-claude-desktop-code-benchmark.md` D14/D16).
Earlier spikes: `2026-11-02-d8-fork-verification.md`, `2026-11-03-d9-rewind-verification.md`,
`2026-11-04-d13-memory-verification.md`.

## 0. Versions and how to read the offsets

- **Binary (dobby).** `~/.local/bin/claude` resolves to `~/.local/share/claude/versions/2.1.318`, whose sha256 starts
  with `708c77b204db3008`. Every byte offset (`@N`) below is into this file. The bundle is minified, so each offset was
  read in context.
- **Binary (rocky).** Live worker sessions run `~/.local/share/claude/versions/2.1.293`. Its strings include
  `apply_flag_settings` (40 hits), `get_sandbox_dialog` (9), `sdk_opt_in_required` (3) and `fast_mode_state` (8).
  Probe R (§6) ran against it.
- **Python SDK.** `claude-agent-sdk` 0.2.105 is in the deploy venv (`/home/tony/GitHub/viberpit-deploy/.venv`).
- **Probes.** The harness is D-9's (`test_rewind_cli_contract._cli_path_env` / `test_memory_cli_contract`):
  - a stream-json CLI with an env built from scratch;
  - a throwaway `CLAUDE_CONFIG_DIR` and `HOME`;
  - `ANTHROPIC_BASE_URL=http://127.0.0.1:9`;
  - control requests only. No user message was written, no `assistant` frame appeared and no model call was made.
  - Scripts lived in `/tmp/d14-scripts/` and every probe dir was removed afterwards.

## 1. Fast mode (⚡ in the prompt row)

### 1.1 What it is (binary)

- **Definition.** Fast mode is a faster **output mode for Opus**. It is the same model, not a downgrade.
  - System-prompt text @292453371: "Fast mode for Claude Code uses Claude Opus with faster output (it does not
    downgrade to a smaller model). It can be toggled with /fast."
  - Help @322576060: "/fast toggles fast mode — same model, faster output."
- **It costs more.** The picker copy @~317994514 reads `High-speed mode for ${model}. Draws from usage credits at a
  higher rate. Separate rate limits apply.`
  - The Fleet View hint @301839027 appends ". It draws from usage credits at a higher rate."
  - The model-switch status line `tUe` @304403624 appends " · Draws from usage credits".
  - Rejection reasons (`M0` @283698901): `out_of_credits`, `org_level_disabled`, `member_level_disabled`,
    `seat_tier_zero_credit_limit`, `overage_not_provisioned`, and others. These all read "Fast mode disabled · …usage
    credits…".
  - `YE("free")` reads "Fast mode requires a paid subscription".
  - **On a subscription account, fast mode bills to usage credits (overage), not to the plan's included usage.**
- **Separate rate limits.** The 429 kinds `fast-mode-limit` (cooldown 600000 ms) and `fast-mode-short-limit` (10 s)
  are at @283622449. A cooldown sets `fast_mode_state: "cooldown"` and the request falls back to standard speed
  (@282854550 description, and the "Fast mode cooldown triggered" strings @157742846).
- **Setting.** `fastMode` is a boolean in the settings schema @280987937: "When true, fast mode is enabled. When absent
  or false, fast mode is off."
  - `fastModePerSessionOptIn` is a policy-restrictive key @280882472.
  - The command is `/fast [on|off]` @292189174. Its terminal variant is `local-jsx`. Its non-interactive variant (`$Qe`)
    has `supportsNonInteractive:!0` and `isEnabled:()=>ye()`.
  - **There is no CLI flag and no `set_fast_mode` control request.** The full list of control subtypes was extracted;
    the only setters are `set_model`, `set_permission_mode`, `set_max_thinking_tokens`, `apply_flag_settings`,
    `update_settings` and similar.
- **Env.**
  - `CLAUDE_CODE_DISABLE_FAST_MODE` turns it off (`Ls()` @~283691700: `firstParty && !DISABLE_FAST_MODE`).
  - `CLAUDE_CODE_SKIP_FAST_MODE_ORG_CHECK` and `CLAUDE_CODE_SKIP_FAST_MODE_NETWORK_ERRORS` exist (@280426158).
  - `CLAUDE_CODE_ENABLE_OPUS_4_7_FAST_MODE` and `CLAUDE_CODE_OPUS_4_6_FAST_MODE_OVERRIDE` are in the string table
    (@152904772).
- **Availability gate `iH()`** (@~283692700). It returns a reason, or `null` when fast mode is available:
  - `not_first_party` or `disabled_by_env`;
  - `model_not_allowed`, when Opus is not in the org's allowed models;
  - `preference`, when policy sets `fastMode:false`;
  - **`sdk_opt_in_required`**, which applies under `ye()&&HGn()&&!h`:
    - `ye()` @280199331 is `!isInteractive()`.
    - `HGn()` @280213012 is `ye() && clientType !== "claude-vscode"`.
    - `h` is `sessionOptIn || flagSettings.fastMode === true`.
    - **So an SDK host such as ViberPit gets fast mode only through the flag layer** (`--settings` or
      `apply_flag_settings`). User-settings `fastMode` is not enough.
  - `pending` / `disabled`, the org-status check.
- **Supported models.** `s_()` @~283695000 reads the catalog capability `fast_mode` and falls back to a name containing
  `opus-4-8` or `opus-5`.
  - The capability arrays carry `fast_mode` for opus-4-8 @281507573, opus-5 @281508565 and opus-5-5 @281509669.
  - One more fast-capable entry @281517763 follows `claude-mythos-5-1` in the table. Which id it belongs to was not
    confirmed.
  - Sonnet, Haiku, Fable and Opus ≤ 4.7 do not have it.
  - At runtime, `initialize.models[].supportsFastMode` is the authority (probe A). It was `true` for `default`/`opus`
    (Opus 5.5) and absent for fable, sonnet, haiku and `claude-opus-4-7`.
- **Where the state is reported.** `fast_mode_state` (`on|off|cooldown`) and `fast_mode_disabled_reason` are on:
  - the **`initialize` response**;
  - `system/init` (@282725265);
  - every **`result`** (@282713186, @282717879);
  - the session-metadata response (@282881933).

### 1.2 SDK (0.2.105)

- There is no fast-mode API: no `fast_mode` option and no `set_fast_mode` method.
- `ClaudeSDKClient` exposes `set_model`, `set_permission_mode` and the other setters, but **not `apply_flag_settings`**.
  ViberPit would send it through `_query._send_control_request`, the private seam D-9 rewind already uses
  (`claude_driver._rewind_control`).
- `ClaudeAgentOptions.settings` maps to `--settings` (the flag layer). `subprocess_cli._build_settings_value` merges
  `options.sandbox` into it as a JSON string.

### 1.3 Probes A to C (dobby 2.1.318)

```
A  no flags              initialize: fast_mode_state "off", fast_mode_disabled_reason "sdk_opt_in_required"
                         apply_flag_settings {fastMode:true} -> success; get_settings.effective {fastMode:true}
                         apply_flag_settings {fastMode:false} -> success; effective {fastMode:false}
B  --settings {"fastMode":true}                 initialize: fast_mode_state "on", reason none
B' --settings {"fastMode":true} --model sonnet  initialize: "off", reason none (model unsupported)
B" ... --model claude-opus-4-7                  initialize: "off"
C  set_model sonnet, then fastMode true/false, one reply awaited each time (1.5 s gaps):
                         each get_settings reflects the preceding write; model stays claude-sonnet-5-5
D  apply_flag_settings {effortLevel:"high"} -> get_settings.applied.effort "medium" -> "high" (Opus 5.5)
```

- **Read-after-write.** Frames written back to back **without waiting for each reply** raced. A `get_settings` answered
  with the state from before the immediately preceding `set_model` or `apply_flag_settings`.
- The SDK awaits every control reply, so a driver that awaits is fine. A batch writer is not.
- **Not observed.** Without a turn, nothing showed whether `fast_mode_state` flips mid-session after
  `apply_flag_settings` (see Gaps). The code path that would do it is `avs()` @283694757, which resyncs
  `appState.fastMode` from settings on change.

### 1.4 ViberPit today

- There is no fast concept anywhere: no hits for `fast_mode`, `fastMode` or `/fast` in `server/` or `web/src`.
- `server/sessions/commands/catalog.py` has `model`, `fork`, `rewind` and `btw`. There is no `/fast` and no `/effort`.
- **D-1 effort is Codex-only.**
  - `ClaudeAgentDriver.set_model` (`claude_driver.py:2286`) ignores `effort`, because
    `AgentCapabilities.reasoning_effort` is False for Claude.
  - `PromptRow.tsx` hides the EffortPicker unless `canUseEngine(caps,'reasoningEffort')` and the catalog row has
    `reasoningEfforts`. Claude's `/api/models` rows carry neither (`server/api/models.py` docstring).
  - The binary, however, applies effort live: in probe D, `apply_flag_settings {effortLevel:"high"}` changed
    `get_settings.applied.effort` from `medium` to `high`. `update_settings` @283038711 says the running session's
    level is set through `apply_flag_settings`. **Spec row 378's "live effort … on the driver" is feasible for Claude
    over the same channel as fast mode.**
- The spawn path is `claude_sdk_factory.py:161`, `settings=_MCP_TRUST_SETTINGS`, which is
  `{"enableAllProjectMcpServers": true}`. The live rocky sessions show it in argv as `--settings {...}`. That is the
  one place a per-session `fastMode` or `sandbox` would be merged at spawn.

### 1.5 Implications

1. **Decision for the user (cost).** Fast mode draws from **usage credits at a higher rate**, outside the Max plan's
   included usage, with separate rate limits. The ⚡ toggle should not be a casual toggle without a cost note.
   Options: allow it with a cost note; require confirmation; or gate it per account in Settings, off by default.
2. **Mechanism.**
   - At spawn, merge `fastMode` into the `--settings` JSON.
   - Mid-session, send `apply_flag_settings {fastMode: bool}` and await the reply.
   - Read the truth from `initialize`/`result` `fast_mode_state` and `fast_mode_disabled_reason`, and surface the
     reason (cooldown, credits) in the UI.
   - Persist the per-session choice on the session row, a new nullable column (migration **0099**), so resume and
     respawn re-apply it.
3. **Visibility.**
   - Show ⚡ only when the engine is Claude, the live model's `supportsFastMode` is true (from the `initialize`
     `models` list, through `server_info`, not a binary scan), and the reason is not
     `not_first_party`/`disabled_by_env`.
   - Hide it for Copilot accounts as a ViberPit policy. The CLI gate checks the provider, not the base URL (see Gaps),
     so it may not hide it by itself.
   - Hide it for Codex. Codex `model/list` has `serviceTiers` (`server/codex/schema.py:1180`) that nothing consumes,
     and no fast semantics were verified.
4. **Model switch.** Switching to a model without fast support leaves `fastMode:true` in the flag layer while the CLI
   reports `off`. The toggle should show the reported state, not the stored setting.
5. **`/fast` app command.** It is an app command in `catalog.py` that dispatches to the same driver call. Without it,
   `/fast` typed as a prompt would reach the CLI's non-interactive `/fast`, which this spike did not run (no user
   messages).

## 2. Sandbox (session default and per-session toggle)

### 2.1 Binary

- **Settings key.** `sandbox` (schema `rIr` @~280860100):
  - `enabled` ("Run Bash commands inside the sandbox. Default: false");
  - `failIfUnavailable` ("When false (default), a warning is shown and commands run unsandboxed");
  - `autoAllowBashIfSandboxed`, which **defaults to true** (@156830180);
  - `allowUnsandboxedCommands` (default true, so the model can pass `dangerouslyDisableSandbox`);
  - `network.{allowedDomains, deniedDomains, strictAllowlist, allowUnixSockets (macOS only), allowAllUnixSockets}`
    (@280839227). Unknown domains **prompt** unless `strictAllowlist` is set;
  - `filesystem`, `credentials`, `excludedCommands`, `enableWeakerNestedSandbox`, `allowNestedUserNamespaces`.
- **No CLI flag.** It is set through settings only. The SDK's `options.sandbox` merges into `--settings`.
  `edit-sandbox-settings` (@297707855) is a hidden subcommand for the VS Code extension that writes files.
- **Reading it.** `get_sandbox_dialog` (@282941833) returns `supported`, `locked`, `enabled` (running),
  `enabled_in_settings`, `unavailable_reason`, `mode` (`auto-allow|regular|disabled`), `dependencies.{errors,warnings}`
  and `restrictions`.
- **Linux requirements.** These are `bubblewrap` and `socat`; the error text says "install missing tools (e.g. apt
  install bubblewrap socat)".
  - **dobby:** bwrap 0.11.1 and socat present, `bwrap --unshare-net true` works, `apparmor_restrict_unprivileged_userns=0`.
  - **rocky:** bwrap 0.11.1 works (`apparmor_restrict_unprivileged_userns=1`, but bwrap still runs), and **socat is
    missing**.

### 2.2 Probes S and R

```
S  dobby, no flags     get_sandbox_dialog: supported, enabled:false, mode "disabled", deps {errors:[],warnings:[]}
                       apply_flag_settings {sandbox:{enabled:true}}  -> enabled:true, mode "auto-allow", locked:true
                       apply_flag_settings {sandbox:{enabled:false}} -> enabled:false, mode "disabled"
S' dobby, --settings {"sandbox":{"enabled":true}}: enabled:true at start; the live off/on toggle works the same
R  rocky 2.1.293, --settings {"sandbox":{"enabled":true},"fastMode":true}
   stderr: "⚠ Sandbox disabled: sandbox is enabled but dependencies are missing: socat not installed … Commands will
            run WITHOUT sandboxing."
   get_sandbox_dialog: enabled:false, enabled_in_settings:true, unavailable_reason "…socat not installed…",
                       dependencies.errors ["socat not installed"]; initialize fast_mode_state "on"
```

- **The sandbox can change mid-session.** `apply_flag_settings` starts and stops it live, so it is not spawn-only.
- `locked:true` after a flag-layer write only means the local `/sandbox` picker cannot override it.

### 2.3 ViberPit today

- Claude has no sandbox setting. `engine_policy.py:241` states it outright: "Claude has no sandbox at all".
- Codex's sandbox is not an independent knob. `server/codex/permissions.py` derives `read-only` / `workspace-write` /
  `danger-full-access` from the ViberPit permission mode, and egress is allowed only in `full-auto`/`bypassPermissions`
  (`permits_network_egress`).
- **Codex therefore does not map onto a per-session sandbox toggle.** Changing it would change the meaning of the
  permission mode.
- Other `sandbox` hits (`x11manager`, `native_display`, `HooksSection`, `useSessionRuntime`) are unrelated.
- The account `settings.json` is shared by symlink across every account (memory "Shared settings model…"). **A
  per-session override must go in the flag layer** (`--settings` JSON or `apply_flag_settings`), never into
  `settings.json`.

### 2.4 Implications and risks

1. **Risk: it fails open.** `failIfUnavailable` defaults to false. On rocky today a "sandboxed" session runs
   **unsandboxed** with only a stderr warning.
   - ViberPit must read `get_sandbox_dialog` after spawn and after every toggle, and show `unavailable_reason` in the
     Environment card.
   - Alternatively, send `failIfUnavailable:true`, which makes the spawn fail.
   - Ops: `apt install socat` on rocky before shipping.
2. **Risk and decision: permissions.**
   - Turning the sandbox on also turns on `autoAllowBashIfSandboxed` (default true, `mode: "auto-allow"`). Sandboxed
     Bash then **skips ViberPit's permission cards**.
   - It is a security posture change, not only a containment one.
   - Decision for the user: send `autoAllowBashIfSandboxed:false` (sandbox plus the usual prompts) or keep the CLI
     default.
3. **Risk: automations break.**
   - Network for sandboxed commands prompts on unknown domains.
   - On Linux, Unix sockets are blocked unless `allowAllUnixSockets` is set (seccomp cannot filter by path). That
     plausibly breaks `gh` (keyring over D-Bus), `ssh-agent` git pushes and docker. Unverified; see Gaps.
   - `engine_policy`'s "automation must be able to push" preflight would need a Claude branch.
   - Recommend that the sandbox default **not** apply to automation-origin sessions in D-14.
4. **Engine gating.** Make it Claude-only through a new capability flag (for example `AgentCapabilities.sandbox`), and
   hide the toggle for Codex. The Codex sandbox stays a function of the permission mode, and the Environment card can
   show it read-only.
5. **Storage.**
   - The session default is an `AppSettings` nullable column, with an env fallback per the `assistant_enabled` pattern.
   - The per-session value is a nullable `sessions` column.
   - Both go in migration **0099**, with `_REQUIRED_EXTRAS`/reconcile lists checked.

## 3. Worktree location and branch prefix

- **Location is already a setting.** Worktrees live at `<repo_root>/viberpit-sessions/worktrees/<clone>/<short-id>`:
  - `worktrees.session_worktree_dir` @`src/viberpit/worktrees.py:478`;
  - `worktrees_base` uses `paths.session_namespace_dir` (`<repo_root>/` + `brand.SESSION_NAMESPACE`).
  - `repo_root` resolves through `orchestrator._effective_repo_root()` (`orchestrator.py:26273`): `AppSettings.repo_root`
    in the DB, then `VIBERPIT_REPO_ROOT`, then `~/GitHub`.
  - It is already editable as **Settings › General › repoRoot** (`GeneralSection.tsx:218`, `api/settings.py:201`).
  - The same root also holds the **clones**, so "worktree location" is not a separate knob today.
- **Hosts.** The rocky worker reads the same row: its daemon env has `VIBERPIT_DB_URL=postgresql+asyncpg://…`, and
  `_effective_repo_root` is per daemon. One fleet-wide path is applied on every host. It works only because both hosts
  use `/home/tony/GitHub`.
- **Branch prefix is a brand constant, not config.**
  - `brand.BRANCH_PREFIX = "viberpit/"` (`src/viberpit/brand.py:75`).
  - Child branches are `viberpit/<base>-<short>` (`worktrees._child_branch`); forks are `viberpit/fork-…`.
  - **`brand.is_session_branch` gates `git branch -D`** in `remove_worktree` (`worktrees.py:1414`).
  - No `VIBERPIT_*` env exists for it.
- **What changing the location would do to existing worktrees.**
  - Nothing moves. Existing `Session.cwd` rows and `gitdir:` pointers keep pointing at the old path.
  - `_managed_roots()` (`orchestrator.py:6583`) recognises only {current effective root, constructor/env root}. A
    worktree under a **previous DB value** stops being "managed": cleanup won't remove it and the janitor
    (`worktrees_janitor._candidate_worktree_dirs`) won't sweep it.
  - The clones are re-made under the new root.
- **Implications.**
  1. Do not add a second location knob. Reuse `repoRoot`: surface it in **Settings › Sessions** (move or alias, one
     source of truth) with copy that explains it covers clones and worktrees and applies to new sessions only.
     Optionally keep previous roots in a list so `_managed_roots` and the janitor keep seeing them.
  2. **Risk: a configurable branch prefix is a deletion hazard.** A prefix of `""`, `feature/` or `main` would let
     `remove_worktree` `git branch -D` the user's own branches.
     - It needs validation: non-empty, ends in `/`, not a protected name.
     - `is_session_branch` must keep matching **every prefix ever used** (current, history and legacy), or old
       branches leak forever (the `brand.py:76-84` rationale).
     - Recommend a stored history list, or deferring the prefix entirely. This is a decision for the user.
  3. Per-host roots, for a rocky path that differs, are out of scope. Flag this if a host ever differs.

## 4. Presets in the dispatch bar

- **Data model** (`server/db/models.py:1348-1387`).
  - `SessionPreset` (`session_presets`): `name` (unique), `repo`, `account`, `model`, `permission_mode`,
    `initial_prompt`, `branch_strategy`, `permission_profile_id`.
  - `PromptTemplate` (`prompt_templates`): `name` (unique), `content` with `{{var}}`, `variables`, `tags`.
  - `PermissionProfile` is related.
- **API.**
  - `/api/presets` CRUD (`server/api/presets.py`).
  - `/api/templates` CRUD plus `POST /{id}/render` (`server/api/templates.py`).
  - **`POST /api/sessions` already accepts `presetId`** (`api/sessions.py:119, 870-896`). It fills only the fields the
    caller left unset.
  - **`branch_strategy` is stored but has no consumer.**
- **Consumers.**
  - The Settings › Presets / Templates sections (CRUD only).
  - The Assistant tools `list_presets`, `list_templates` and `render_template`, and `create_session` with `preset`.
  - The web dispatch path never sends `presetId`. `hooks.ts:455` declares it but nothing sets it.
- **The DispatchBar has no `+` menu.**
  - The row is: textarea, ✨ assess, mic, a 📎 "Attach image" button (`DispatchBar.tsx:882`), and Create.
  - "More options" (`:621`) is a disclosure for engine, account and similar.
  - The in-session `Composer` also has only an "Attach image" button (`Composer.tsx:581`).
  - The spec's `+` menu (attach, slash commands, presets) is new UI in both places.
- **Chord.**
  - The reserved and bound list is `web/src/shell/shortcuts.ts`, pinned by `shortcuts.test.ts:1038` ("the un-bindable
    Desktop chords bind nothing").
  - Every digit family is taken or unsafe:
    - ⌘/Ctrl+1–9 is browser tab selection.
    - ⌥/Alt+1–9 is Linux tab selection.
    - ⌘⌥1–9 / Alt+Shift+1–9 is the D-10 session jump.
    - ⌘⇧3/4/5 is macOS screenshots, never delivered to the page.
  - **Recommendation: no preset chord.** Put presets in the `+` menu and as "New session from preset: <name>" entries
    in the ⌘K palette (D-10's registry), which is keyboard-reachable without a new chord.
- **Implications.**
  - Picking a preset in the `+` menu should pre-fill the visible DispatchBar fields and send `presetId`, so the server
    rule "explicit wins" holds.
  - A template inserts its rendered text, through `/render` for variables.
  - Either drop `branch_strategy` from the UI or define it (Principle VI: no dead fields).

## 5. PR auto-fix per-session toggle

- **PR Doctor** (`server/automations/pr_doctor.py`; the behaviour is in the orchestrator):
  - `_on_pr_change` (`orchestrator.py:16492`) routes a CI transition to the session that owns (repo, branch).
  - `_pr_doctor_sweep` runs every poll cycle.
  - Both reach `_pr_doctor_step` (`:17024`). Its gates are, in order:
    1. the automation is enabled;
    2. `pr_autofix` mode;
    3. config `enabled_repos`;
    4. the target is terminal;
    5. `plan_recovery(classify(info), mode, config)`;
    6. the per-head dedupe;
    7. `_backlog_pr_doctor_active(repo)`;
    8. then Mechanical, SessionFix, Propose or GiveUp.
  - **No per-session flag is consulted**, and `rec.autopilot_paused` is not read on this path.
- **Autopilot** skips sessions with `autopilot_paused` (`orchestrator.py:15607`, revalidated at `:16050/16069`). It nudges
  and approves; it does not fix PRs.
- **Existing per-session flags.**
  - `sessions.autopilot_paused` (`models.py:179`): API `POST …/autopilot` (`api/sessions.py:1539`), Assistant tool
    `set_autopilot_paused`, and the SessionCard menu "Pause/Resume autopilot".
  - `auto_archive_opt_out_reason`.
  - `owns_pr` is a draft-ready guard in the hooks (`claude_sdk_factory`), not an auto-fix flag.
  - There is no `pr_doctor` opt-out.
- **Environment card** (`web/src/features/sessions/rail/EnvironmentCard.tsx`, 79 lines): Agent, Account (switcher),
  Host, Display, Branch, Worktree (copyable) and `PrList`. It has **no** open-PR, discard or repair actions and **no**
  sandbox or auto-fix rows, all of which spec line 65 expects.
- **Implications and decision for the user.** "No new mechanism" leaves two choices:
  - **(a)** Reuse `autopilot_paused` as the toggle and add one `rec.autopilot_paused` check in `_pr_doctor_step`. This
    is one column, but pausing PR fixes also pauses autopilot help, and the reverse.
  - **(b)** Add a nullable `sessions.pr_autofix_paused` read at the same single chokepoint (`_pr_doctor_step`, before
    `plan_recovery`).
  - Either way the check covers **SessionFix and Mechanical**. Whether branch updates should still run is part of the
    decision. Notifications ("PR broke") stay.
  - Recommend (b), because it keeps the two semantics apart. The skip should be reported once through
    `_pr_doctor_report_skip`.
  - Note that the sweep may find the PR with no live record (`_pr_doctor_owning_rec`), so the flag has to be read from
    the **DB row**, not only `rec`.

## 6. Housekeeping facts

- Bundle ceilings (`web/bundle-budget.json`): entryJs **107.8**, initialJs **302.7** (gzip kB).
- Latest migration: **0098** `0098_session_memory_files.py`. D-14's new columns go in 0099.
- Assistant tools: 114 registered and 107 exposed (`docs/product/features/ai-assistant.md:143`). If D-14 adds tools
  (for example `set_fast_mode`, `set_sandbox`, `set_pr_autofix`), update the counters, the eval digest and the ceiling
  (memory: parallel assistant-tool PRs conflict on these).

## Gaps (not verified)

- Whether `fast_mode_state` changes **mid-session** after `apply_flag_settings {fastMode:true}`. Only spawn-time
  (`initialize`) was observed; confirming it needs a turn (`result.fast_mode_state`), which means a model call.
- The **price multiplier** of fast mode. The binary says only "a higher rate" plus separate rate limits; no number was
  found.
- The id of the fourth `fast_mode` capability row (@281517763), and whether Copilot or third-party base URLs always
  give `not_first_party`. The probe used a custom loopback URL and still got `sdk_opt_in_required`. So the check is on
  the provider (`Ae()`), not the URL, and a Copilot proxy session may be treated as first party.
- Whether `autoAllowBashIfSandboxed` still auto-allows when the sandbox **failed to start** (rocky case R).
- The real effect of the Linux sandbox on `gh`, `git push` over ssh-agent, docker and the session's Xvfb/VNC. No
  sandboxed command was run.
- How a sandbox network "prompt" for an unknown domain reaches an SDK host: through `can_use_tool` or some other
  request.
- The CLI's non-interactive `/fast` local command (chunk `jq8w4f0j`) was not exercised, because it needs a user
  message.
- Codex fast or service tiers (`serviceTiers` in `model/list`). Not probed.
