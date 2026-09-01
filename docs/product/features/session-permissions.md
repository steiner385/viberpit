---
slug: session-permissions
title: Session Permissions & Model Gates
state: live
links:
  code:
    - server/sessions/permissions.py
    - server/sessions/types.py
    - server/sessions/model_policy.py
    - server/sessions/model_catalog.py
    - server/api/permission_profiles.py
    - server/api/dispatch.py
    - server/api/settings.py
    - server/sessions/driver.py
    - web/src/components/session/PendingPermissionPanel.tsx
    - web/src/components/session/PermissionCard.tsx
  prs: [758, 875, 862, 876, 858, 605, 232, 155]
---

# Session Permissions & Model Gates

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
isolation feature). Tool-pattern strings in profiles are stored as-is and applied client-side, not
server-validated.

**Notes**
- **Permission modes.** `VALID_SESSION_MODES` (`server/sessions/types.py`) is the single source of
  truth: `interactive` (prompt every call), `auto` (auto-approve everything except a dangerous
  denylist — Bash/Edit/Write/MultiEdit/NotebookEdit/WebFetch/WebSearch), `allowlist` (auto-approve
  read-only Read/Grep/Glob/LS, prompt the rest), `full-auto` (auto-approve all incl. shell),
  `acceptEdits` (auto-approve file writes, prompt shell/network), and `bypassPermissions`.
  `permissions.py::PermissionRegistry` enforces these; `to_sdk_permission_mode()` maps them onto the
  SDK's own `default`/`plan`/`acceptEdits`/`bypassPermissions` (#758, #232, #200/#84 auto-memory).
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
  presets/templates/profiles set (#605).
- **Model-approval gate.** `model_policy.py::resolve_model_policy` classifies a requested model's
  tier (via `model_catalog.py`, which probes the `claude` binary's alias list — opus/sonnet/haiku/
  fable) and, using the same account→global→env precedence, returns `allow`, `require_approval`, or
  `block`. A gated model creates a driverless `pending_approval` row (`pending_model` set, no
  capacity slot) until an admin approves or denies; mid-session `set_model` respects the same gate
  and swaps at the next turn boundary (#876, #538, #857).

**Evidence** — Functional non-prompting permission modes + unified SDK mapping (#758); exiting plan
mode restores the prior permission mode (#875); adopted sessions honour the default permission mode
via one shared resolver (#862) and API honours the global default when the request omits one (#858);
model-approval gate holding gated models (fable) pending human approval (#876); power-user presets,
templates, and permission profiles (#605); never prompt for MCP tools in the most permissive setup
(#232); Plan-mode toggle in the session settings strip (#155). Code:
`server/sessions/permissions.py`, `types.py`, `model_policy.py`, `model_catalog.py`;
`server/api/permission_profiles.py`, `dispatch.py`, `settings.py`; `server/sessions/driver.py`;
`web/src/components/session/PendingPermissionPanel.tsx`, `PermissionCard.tsx`.
