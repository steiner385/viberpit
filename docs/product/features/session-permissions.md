---
slug: session-permissions
parent: security-and-isolation
title: Session Permissions & Model Gates
state: live
links:
  code:
    - server/sessions/permissions.py
    - server/sessions/types.py
    - server/sessions/model_policy.py
    - server/sessions/model_catalog.py
    - server/api/permission_profiles.py
    - server/permission_rules/
    - server/api/permission_rules.py
    - server/api/dispatch.py
    - server/api/settings.py
    - server/sessions/driver.py
    - web/src/components/session/PendingPermissionPanel.tsx
    - web/src/components/session/PermissionCard.tsx
  prs: [758, 875, 862, 876, 858, 605, 232, 155]
---

# Session Permissions & Model Gates

Keep session actions and model choices within consistent, inspectable permission and approval rules.

**Problem** — An autonomous `claude` session can run shell, edit files, fetch the web, and switch
models. Different sessions warrant different amounts of trust: some should prompt for every
dangerous action, some should run fully unattended, some should plan first and execute only on
approval. Without a governed, consistent policy, a session either nags on every safe call or runs
wide-open by accident; a "Plan mode" toggle that dropped back to bare `default` on exit would
silently strip a full-auto session of its autonomy; and an expensive or restricted model tier could
be spun up with no human gate.

**Target users** — Operators configuring how much latitude a session's agent has — from
prompt-on-everything to full-auto — including power users who save reusable permission profiles;
admins who want expensive/restricted model tiers held for approval before a session can use them.

**Desired outcome** — Every session materializes with a permission mode resolved through one shared
precedence chain, so the API, adoption, and automation paths all agree. Plan mode is a reversible
overlay that restores the *prior* mode on exit, not bare `default`. Named permission profiles let a
user apply a mode + tool-pattern bundle in one click. Gated model tiers are held at
`pending_approval` (consuming no capacity) until an admin approves, and mid-session model changes
respect the same gate.

**Success metrics** — (unknown — needs owner input) for a headline target. In-code signals the team
tracks: one shared resolver for the default permission mode across create/adopt/automation (#862,
#858); exiting plan mode restores the prior mode, never `default` (#875); functional non-prompting
modes with a unified SDK mapping (#758); gated models (e.g. `fable`) held pending human approval
(#876).

**Non-goals** — Not a sandbox or a hard security boundary against a malicious agent — permission
modes govern which tool calls auto-approve vs. prompt within the Claude Agent SDK, not OS-level
isolation (that is the config-dir/process isolation of constitution Principle II and the worktree
isolation feature). Tool-pattern strings in *named* profiles are stored as-is and applied client-side, not
server-validated (and not yet enforced); standing always-allow rules are the validated, enforced
exception (see Standing rules).

**Notes**
- **Permission modes.** `VALID_SESSION_MODES` (`server/sessions/types.py`) is the single source of
  truth: `interactive` (prompt every call), `auto` (auto-approve everything except a dangerous
  denylist — Bash/Edit/Write/MultiEdit/NotebookEdit/WebFetch/WebSearch), `allowlist` (auto-approve
  read-only Read/Grep/Glob/LS, prompt the rest), `full-auto` (auto-approve all incl. shell),
  `acceptEdits` (auto-approve file writes, prompt shell/network), and `bypassPermissions`.
  `permissions.py::PermissionRegistry` enforces these; `to_sdk_permission_mode()` maps them onto the
  SDK's own `default`/`plan`/`acceptEdits`/`bypassPermissions` (#758, #232, #200/#84 auto-memory).
- **Standing rules ("always allow").** A rule is one pattern string stored in an auto-managed,
  scoped `PermissionProfile` (`always-allow:*`, hidden from the named-profile API) with a
  `PermissionRuleStats` row for id, author and hit counters. Code: `server/permission_rules/`
  (`patterns.py` grammar/matcher, `safety.py` assessment, `store.py`, `gate.py` enforcement),
  `server/api/permission_rules.py`, `session_actions.grant_always`.
  - *Grammar.* Claude-Code style: `Tool`, `Tool(spec)`, `mcp__server[__tool]`. Bash accepts an
    exact command, a `prefix:*` form, or a glob where `*` is allowed **only at the end, after a
    space** (whole words: `npm run *`, never `np*`). Quoting,
    escapes and variables are refused in Bash rules, and compound commands are matched per
    segment (each must be covered). Path tools (Read/Edit/Write/MultiEdit/NotebookEdit) take
    worktree-relative globs; WebFetch takes `domain:host`.
  - *Scopes.* `session`, `repo`, `global`. A repo or global rule reaches **every** session on
    that repo or the whole fleet (all accounts and hosts), including sessions already running.
    A session's own rules are hidden from the full rules list while it is archived and deleted
    with it on permanent delete (`store.delete_session_rules`).
  - *Human-only.* Creating, editing or revoking a rule needs a person in the UI. The assistant
    and other token-borne callers are refused (`resolve_permission` is allow/deny only), and a
    grant refuses a card owned by another identity. Legacy plain "always" is now a **session**
    rule built from the card's first `ok` suggestion (plain allow when none exists or rules are
    off).
  - *Safety tiers.* `assess()` returns refuse / warn / ok. Refused in a prefix/glob head:
    - exec wrappers and interpreters (`python`/`python3` only as `-m <module>`, and never with an
      inline-code module: `timeit`, `pdb`, `code`, `runpy`, `cProfile`, `profile`, `trace`,
      `IPython`, `idlelib`);
    - package runners, whatever follows, because the next word is a package fetched from the
      network: `npx`, `bunx`, `uvx`, `npm exec`/`x`, `pnpm dlx`, `yarn dlx`, `pipx run`,
      `uv tool run`, `pnpx`, and the scaffolders `npm init`/`create`, `pnpm create`,
      `yarn create` (an exact command such as `Bash(npx tsc --noEmit)` can still be stored);
    - local command launchers (`uv run`, `pnpm exec`, `yarn exec`, `poetry run`) unless followed
      by the command they run, which is then judged as a head of its own (`uv run pytest:*` ok,
      `uv run python:*` refused);
    - a package manager that can launch arbitrary commands (`npm`, `pnpm`, `yarn`, `uv`,
      `poetry`, `pipx`, `bun`, `deno`, `uvx`, ...) with no subcommand after it, an option first,
      or a head that stops partway into a launcher (`Bash(npm:*)`, `Bash(npm *)`,
      `Bash(npm --prefix x:*)`, `Bash(uv tool:*)`; `Bash(npm test:*)`, `Bash(uv sync:*)` and
      `Bash(npm run *)` stay ok);
    - `git push` except exactly, `git -c` (incl. the attached `-cKEY=VAL` form), `--config-env`,
      `--exec-path` and `git config`.

    Every Bash rule is also probed against representative dangerous commands. WebFetch domains
    that are wildcards, single-label, IP literals or internal suffixes (`.local`, `.internal`,
    ...) are refused. Broad-but-legal patterns need
    `confirm`. A hard `PROTECTED_PATHS` exclusion (`.git`, `.claude`, `.env*`, keys, ...) means
    no rule can ever match those paths. Path matching resolves symlinks and never matches
    outside the worktree. A WebFetch domain rule only matches a plain `http`/`https` URL: one with
    userinfo (`user@`), a backslash, whitespace or control characters, or a non-numeric port is
    never matched (Python's and Node's URL parsers disagree on those) and is always carded.
  - *Enforcement.* `StandingRuleGate` is consulted wherever a card would otherwise be created,
    in **every** mode including `interactive`; it fails closed (any error = ask the human).
    Hits are counted and audited; a compound Bash command covered by several rules credits
    each of them (`patterns.covering`). `patterns.NEVER_RULED_TOOLS` is never matched, suggested or
    storable, in any mode: `ExitPlanMode`, `AskUserQuestion`; the app's own tools that widen
    what sessions may do unasked (`resolve_permission`, `set_permission_mode`,
    `resolve_model_approval`, `set_autopilot_paused`, `set_automation_enabled`); and those that
    settle a human-in-the-loop decision (`answer_question`, `resolve_decision`, `resolve_triage`,
    `advance_initiative`, and the session tools `roadmap_resolve`, `roadmap_advance`). App
    tools are covered under both brand prefixes. A server-wide rule for the app's own MCP server (`mcp__viberpit`, or its
    legacy name) is refused at every scope.
  - *Codex approvals.* `PermissionRegistry.request_external` consults the same gate, so a rule
    answers a Codex command approval with no card. Codex sends the argv it will exec
    (`/bin/bash -lc 'npm test'`); rules match the script inside a plain single- or double-quoted
    `sh`/`bash`/`zsh -c` wrapper (`patterns.unwrap_shell`), and anything else stays wrapped and
    unmatchable. A Codex card offers the rule chooser, and a grant sweeps matching Codex cards.
    Edit-root (`grant_root`) approvals name no file, so no rule covers them; their "Always" is
    still Codex's own session-scoped `acceptForSession`.
  - *Launch profiles.* A named profile chosen at launch is recorded in `Session.tool_allowlist`
    and enforced by the gate after the session's own rules and before repo/global ones. Each
    pattern is re-assessed at session scope on every check and a refused one is skipped; its hits
    are audited (`scope: "launch"`) but have no counter.
  - *Kill switch.* `VIBERPIT_PERMISSION_RULES_ENABLED=false`: nothing is consulted or
    suggested, the rules routes 404, and every prompt cards as before.
  - *Grant cache removed.* The old in-memory 1-hour "always" grant cache is gone; the stored
    session rule replaces it and survives restarts.
- **Plan mode.** `plan` is a temporary overlay, not persisted. `SessionDriver.set_permission_mode`
  saves the current mode in `_pre_plan_mode` on entering `plan`, and on exit (`default` sentinel)
  restores that prior mode — so a full-auto session returns to full-auto, not to prompt-everything
  (#875, #167, #155).
- **Default-mode resolution.** `resolve_permission_mode()` is the one resolver used by every spawn
  path with precedence: explicit request → per-account ledger default → global `AppSettings`
  default → `interactive`. Adopted sessions run through it too, so a takeover honors the operator's
  account-level default instead of defaulting to prompt-everything (#862, #858).
- **Permission profiles.** `server/api/permission_profiles.py` is CRUD for named bundles of
  (mode, tool-pattern list) that a user applies when configuring a session — part of the power-user
  presets/templates/profiles set (#605). The patterns auto-allow like a session rule set (see
  *Launch profiles* above), so writing them is human-only and a pattern the safety check refuses
  is a 400 (`pattern_refused`, with per-pattern reasons).
- **Model-approval gate.** `model_policy.py::resolve_model_policy` classifies a requested model's
  tier (via `model_catalog.py`, which probes the `claude` binary's alias list — opus/sonnet/haiku/
  fable) and, using the same account→global→env precedence, returns `allow`, `require_approval`, or
  `block`. A gated model creates a driverless `pending_approval` row (`pending_model` set, no
  capacity slot) until an admin approves or denies; mid-session `set_model` respects the same gate
  and swaps at the next turn boundary (#876, #538, #857).
- **"Default" is gated on what it launches.** Both gates judge `model_policy.launch_model()`, not
  the raw request: a session that names no model (or the picker's `default` row) still launches on
  the model the `claude` binary reads for itself — `ANTHROPIC_MODEL`, then the `model` key of the
  shared `settings.local.json` / `settings.json` that every account's config dir symlinks from
  `paths.shared_base()`. A `/model` pick in any session rewrites that key fleet-wide, so "Default"
  was silently whatever the last pick left behind (Fable 5.1, 2026-09-08). When the inherited
  default is gated the hold carries its concrete id in `pending_model` with a
  `pending_model_reason` saying nobody chose it, so the approver can counter-offer; when it is
  allowed the session still records `model=None` (account default). Codex never inherits.

**Evidence** — Functional non-prompting permission modes + unified SDK mapping (#758); exiting plan
mode restores the prior permission mode (#875); adopted sessions honour the default permission mode
via one shared resolver (#862) and API honours the global default when the request omits one (#858);
model-approval gate holding gated models (fable) pending human approval (#876); power-user presets,
templates, and permission profiles (#605); never prompt for MCP tools in the most permissive setup
(#232); Plan-mode toggle in the session settings strip (#155). Code:
`server/sessions/permissions.py`, `types.py`, `model_policy.py`, `model_catalog.py`;
`server/api/permission_profiles.py`, `dispatch.py`, `settings.py`; `server/sessions/driver.py`;
`web/src/components/session/PendingPermissionPanel.tsx`, `PermissionCard.tsx`.
