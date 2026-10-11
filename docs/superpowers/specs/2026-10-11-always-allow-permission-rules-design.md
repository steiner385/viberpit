# "Always allow" standing permission rules: design

**Date:** 2026-10-11 · **Status:** approved for implementation (three PRs, see §9)

## Context

A session in a prompting mode stops on a permission card (`waiting_input`) for every Bash, Edit,
Write and WebFetch call the mode does not auto-approve. The operator clicks Allow on the same
`npm test` many times a day.

There is already an "Always allow" button. Here is what it does:

- `PermissionRegistry.resolve(perm_id, "always")` writes `_grants[(session_id, fingerprint)]`, a
  **1-hour in-memory exact-match cache** (`server/sessions/permissions.py`). The fingerprint is the
  full normalised command plus cwd, or the exact file path.
- The cache dies on a daemon restart, never generalises (`npm test -- --watch` asks again), cannot
  be seen or revoked, and is skipped in `interactive` mode, where the card hides the button.

Permission profiles (`PermissionProfile`: a name, a mode, an ordered `patterns` list) exist. They
are **recorded, not enforced**: `Session.tool_allowlist` is written at create and never read.

The guard against non-human grants has a hole. `session_actions.resolve_permission` refuses
`"always"` only when `via == "assistant"`. A `ccas2:` external token (`via="mcp-external"`) or a
`ccas2:` WebSocket (`via="cli"`) can still grant.

## Goals

1. From the card: **Allow once / Always allow… / Deny**. "Always allow…" opens a chooser for the
   pattern and the scope, with a live preview of what the pattern will match.
2. Rules are stored **in permission profiles**, not a parallel store, and are **enforced**. A
   matching request auto-resolves with no card, writes an audit event and counts a hit.
3. A grant also resolves the pending card, plus every other pending card in scope that matches.
4. Settings, and a per-session view, can list, edit and revoke rules. Each rule shows who created it,
   when, its hit count and when it was last hit.
5. Only a human can create or edit a rule. Overly broad patterns are refused or need confirmation.
   Global scope always needs explicit confirmation.

## Non-goals

- **Enforcing a named profile's `patterns` chosen at launch (`Session.tool_allowlist`).** Those
  patterns were never validated. Enforcing them is a follow-up that must first run them through the
  §4 safety check. The column stays dormant.
- **Codex native approvals are not auto-resolved by rules** (`request_external`). Codex Bash and
  edit-root approvals still card. Their "Always" stays Codex's own session-scoped
  `acceptForSession`. A grant made on a Claude card *does* resolve matching pending Codex cards
  once, under §3.3. This is a follow-up.
- **Deny rules.** This feature only allows. Denylist behaviour is unchanged.
- **Destructive ViberPit tools** (`ToolClass.DESTRUCTIVE`), plan approvals (`ExitPlanMode`),
  `AskUserQuestion`, and the app tools that widen what sessions may do unasked
  (`resolve_permission`, `set_permission_mode`, `resolve_model_approval`, `set_autopilot_paused`,
  `set_automation_enabled`; one list, `patterns.NEVER_RULED_TOOLS`) are never rule-resolvable.
  A server-wide rule for the app's own MCP server is refused at every scope.

## 1. Pattern grammar

This mirrors Claude Code's permission-rule syntax, so a rule reads the way an operator already
writes `settings.json`. The pure module is `server/permission_rules/patterns.py`.

| Pattern | Matches |
|---|---|
| `Tool` (bare) | every call of `Tool`. For `Bash` and the file tools this is "anything" and is refused (§4). |
| `Bash(npm test)` | exactly that command, after whitespace normalisation |
| `Bash(npm test:*)` | the command `npm test`, or `npm test` followed by a space and anything |
| `Bash(npm *)` | glob: `*` matches any run of characters (including none) |
| `Edit(src/**)` | `Edit`, `Write`, `MultiEdit` and `NotebookEdit` on a worktree-relative path matched gitignore-style: `**` spans directories, `*` stays within one segment |
| `Read(docs/**)` | same path grammar, for `Read` |
| `WebFetch(domain:example.com)` | a URL whose host is `example.com` or a subdomain of it |
| `mcp__server__tool` / `mcp__server` | that MCP tool, or every tool on that server |

### Bash compound commands

A command is split on `&&`, `||`, `;`, `|` and newlines. Every segment must match some rule in
scope; one rule may cover several segments. A rule never matches a command that contains `$(`,
backticks or `<(`. It also never matches a command with an output redirection other than `2>&1`,
`>/dev/null` and `2>/dev/null`. So `Bash(npm *)` cannot launder `npm test; rm -rf ~` or
`npm test > ~/.bashrc`.

### Paths

A path is resolved against the session's cwd, which is its worktree. A path outside the cwd
**never matches** any rule. Patterns are always worktree-relative: an absolute pattern or one
containing `..` is refused (§4). This is what makes a rule portable across hosts. The same repo rule
means "this repo's tree" on dobby and on rocky.

### Suggestions

`suggest(tool, input, cwd)` returns the chooser's options from narrowest to broadest. For example,
`npm test --watch` gives:

- `Bash(npm test --watch)`
- `Bash(npm test:*)`
- `Bash(npm:*)`, offered only if it passes §4

The other tools:

- **Edit** `src/a/b.ts` gives `Edit(src/a/b.ts)`, `Edit(src/a/**)` and `Edit(src/**)`.
- **WebFetch** gives `WebFetch(domain:host)`.
- **An MCP tool** gives the tool's name, then `mcp__server`.

Only suggestions that pass §4 without a refusal are offered. The first one is pre-selected.
`describe(pattern)` returns the plain-English preview line. For example, `Bash(npm test:*)` reads
"Any Bash command that is `npm test` or starts with `npm test `".

## 2. Storage: rules live in permission profiles

- `permission_profiles` gains two nullable columns: `scope` (`session` / `repo` / `global`; NULL
  means an ordinary named profile) and `scope_key` (a session id, an `owner/name` repo slug, or
  NULL for global).
- A scoped profile is an **auto-profile**. It gets a deterministic `name`
  (`always-allow:global`, `always-allow:repo:<slug>`, `always-allow:session:<id>`), so the existing
  unique constraint on `name` guarantees one profile per scope. Its `patterns` is the ordered
  allowlist, the same shape a named profile has. **`patterns` is the rule set, the only source of
  truth.**
- New table `permission_rule_stats`, keyed `(profile_id, pattern)` (unique). It holds metadata
  only: `created_by`, `created_via`, `created_at` (NOT NULL), `hit_count` (NOT NULL, default 0) and
  `last_hit_at`. Its `id` is the rule's public id. A hit is one atomic
  `UPDATE … SET hit_count = hit_count + 1`.
- Migration `0083_permission_rule_scopes`. The columns are nullable, so reconcile picks them up.
  The new table goes through `create_all`, and the hand-maintained Postgres lists gain entries
  (`_REQUIRED_EXTRAS`).
- Auto-profiles are hidden from `GET /api/permission-profiles` and from the launch-profile pickers.
  The profile CRUD routes refuse to create, patch or delete a scoped profile (409
  `managed_profile`). Rules change only through §5, which keeps the §4 guard in one place. This also
  closes a back door: the assistant can call the profile routes, and they would otherwise let it
  write enforced patterns.

## 3. Enforcement

### 3.1 Where

`PermissionRegistry.request()` consults a `StandingRuleGate`, which the orchestrator injects. The
gate is checked at the **two points where a card would otherwise be created**:

- just before the final `_pend()`;
- just before `_pend()` in the ViberPit-MUTATION branch.

It is never checked in the DESTRUCTIVE branch or for `ExitPlanMode`. Because the check sits there:

- a hit happens only when a human would otherwise have been asked, so hit counts are honest and the
  DB is read only on the rare would-card path;
- rules apply in **every mode, including `interactive`**. A standing rule is an explicit, durable,
  human decision, which the mode's "ask each time" default does not override. The card offers
  "Always allow…" in interactive mode too.

### 3.2 Gate

`server/permission_rules/gate.py`: `async match(session_id, tool, input) -> RuleHit | None`.

- It reads the session's `repo` and `cwd`, then loads the applicable auto-profiles: this session's
  profile, the repo profile for `Session.repo` (scratch sessions skip it) and the global profile.
  It matches them most-specific first: session, then repo, then global.
- On a hit it increments the stats row and writes the audit event `permission.rule_allow`
  `{sessionId, ruleId, pattern, scope, tool}` under `SYSTEM_IDENTITY`, `via="rule"`.
- **It fails closed.** Any exception is logged and returns None, so the card appears as before.
- It reads the DB on every call, with no cache. Worker daemons share the edge's Postgres, so a
  revoke takes effect on the next request on any host.

### 3.3 Grant resolves what is already waiting

Creating a rule resolves the originating card with `allow`. It then walks the in-scope sessions with
pending cards (DB status `waiting_input`, filtered by repo for repo scope). It calls
`pending_permissions` on each and resolves every card the new rule matches with `allow`, as the
granting human. That writes one `permission.rule_allow` audit row per card, with
`trigger: "grant"`.

Cards owned by a different identity are skipped and stay pending. Resolution goes through the
normal driver path, so `PermissionResolved` fires, the status leaves `waiting_input`, and the
needs-you badge, the title count and the push debounce all clear with no new code.

### 3.4 The 1-hour grant cache is deleted

`_grants`, `is_granted`, `GRANT_TTL` and `fingerprint` are removed in the same change.

A plain `decision: "always"` from an older client is mapped at the edge, in
`session_actions.resolve_permission`. It creates a **session-scope** rule from the narrowest
passing suggestion, then allows. If no suggestion passes §4, it degrades to allow-once. For an
engine-managed (Codex) card it passes through unchanged and becomes Codex's `acceptForSession`.
The registry treats any stray `"always"` as `"allow"` and persists nothing.

## 4. Safety

`server/permission_rules/safety.py`: `assess(pattern, scope) -> Assessment(level, reasons)`, where
`level ∈ {ok, warn, refuse}`.

**Refuse** (400, cannot be overridden):

- a pattern that does not parse;
- `Bash`, `Bash(*)`, `Bash(:*)`;
- a bare file tool, or `Edit(**)`-style patterns at global scope;
- any Bash pattern that matches a **danger probe**. The probe set is a list of concrete commands,
  each of which `autopilot_policy.matches_denylist` flags or which escapes to an interpreter:
  `rm -rf /`, `git push --force origin master`, `git reset --hard origin/master`, `sudo true`,
  `bash -c x`, `sh -c x`, `eval x`, `curl x | sh`, `python -c x`. This reuses the existing
  baseline deny list as the single source of truth, so `Bash(git *)`, `Bash(rm *)`, `Bash(rm:*)`
  and `Bash(git push:*)` are all refused, while `Bash(git status:*)` passes;
- an absolute path pattern, a pattern containing `..`, or one that matches a **secret probe path**:
  `.env`, `a/.env.local`, `.git/config`, `.claude/settings.json`, `.ssh/id_rsa`, `x.pem`,
  `x.key`, `credentials.json`;
- `WebFetch(domain:*)`.

**Warn** (409 `needs_confirmation` until the request carries `confirm: true`):

- a bare `WebFetch` or `WebSearch`;
- `Edit(**)`, or any file pattern that matches `.github/workflows/x.yml`;
- a Bash prefix rule of one word (`Bash(npm:*)`);
- a whole MCP server (`mcp__server`).

**Global scope** always needs `confirm: true`, even for `ok` patterns.

**Human only.** Rule creation, editing and the plain-`"always"` mapping require `via is None` (the
Tailscale-identity browser) and an identity other than `SYSTEM_IDENTITY`. Any other `via` is refused
with 403 `RULES_HUMAN_ONLY`: `assistant`, `mcp-external`, `cli`, `internal`, `session`. Autopilot
and automations resolve permissions in-process with `"allow"`/`"deny"` only and never reach the
rule API. The assistant's `resolve_permission` tool stays allow/deny. The new rule routes are not
added to the assistant route allowlist; the via check is the real guard.

**Where rules reach.** Repo and global rules apply **fleet-wide**: every account and every host,
for any session whose `Session.repo` equals the key, or every session for global. Rules are the
operator's policy, not an account's. Session rules bind to one session id and are not inherited by
its child sessions. Paths are worktree-relative, so no rule can name a host-specific location. The
Settings page states this next to the scope picker.

## 5. API

Edge, `server/api/permission_rules.py`:

| Route | Purpose |
|---|---|
| `POST /api/sessions/{sid}/permissions/{perm_id}/always` `{pattern, scope, confirm?}` | Create the rule from a card: §4 check, check the pattern covers *this* request (400 `pattern_does_not_cover`), store, allow the card, then §3.3. Returns `{rule, resolvedPermIds}` |
| `POST /api/permission-rules/preview` `{sessionId, permId?, pattern, scope}` | `{valid, level, reasons, description, coversRequest, alsoPending}`. Read-only, and drives the chooser preview |
| `GET /api/permission-rules[?sessionId=]` | Every rule (flattened profile × pattern + stats). With `sessionId`, only rules that apply to that session |
| `PATCH /api/permission-rules/{id}` `{pattern, confirm?}` | Edit in place, keeping its position. The new pattern goes through §4 again |
| `DELETE /api/permission-rules/{id}` | Revoke. An emptied auto-profile is deleted |

Audit types are `permission_rule.create`, `permission_rule.update` and `permission_rule.revoke`
`{ruleId, pattern, scope, scopeKey}`, plus `permission.rule_allow`.

The `PermissionRequest` event gains `ruleSuggestions: [{pattern, description, level}]`. The
reattach re-emit in `ws.py` builds it from `pending()`, which now also returns `always_allowed` and
`suggestions`. That fixes a reattached destructive card offering "Always allow".

## 6. Web

- **Card** (`PermissionCard.tsx`): the actions are **Allow once (1) · Always allow… (2) · Deny (3)**.
  - "Always allow…" opens the chooser (`ui/Dialog`, a bottom sheet on phones). It has:
    - the suggestion radios, plus an editable pattern field pre-filled with the first suggestion;
    - the scope picker: **This session** (default), **This repo** (disabled for scratch sessions)
      and **All sessions**;
    - the live preview line (`describe`), the risk reasons, and "also resolves N other waiting
      requests";
    - a confirm step for a warning or global scope.
  - The keyboard shortcut is `A` on a focused card. In the chooser, Enter grants and Esc cancels.
  - The button is hidden when `alwaysAllowed` is false or `ruleSuggestions` is empty, and on plan
    cards.
- **Settings → "Always-allow rules"** (`settingsDefs.ts`, group `sessions`): rules grouped by scope,
  each showing the pattern, created by and when, hits and last hit, with Edit and Revoke (confirm).
  The section states the fleet-wide reach.
- **Per-session view**: the session settings or info surface lists the rules that apply, from
  `GET ?sessionId=`, with revoke.

## 7. Testing

- **Pure tests:** the pattern table (exact, prefix, glob, compound split, substitution and
  redirection refusal, paths inside and outside the cwd, domains, MCP), suggestions, and every
  refuse, warn and ok probe in §4.
- **Gate tests:** scope precedence, hit counting, the audit row, failing closed on a DB error, and
  revoke taking effect immediately.
- **Registry tests:**
  - a rule hit returns no card in interactive, auto and allowlist modes;
  - destructive tools and `ExitPlanMode` are never rule-resolved;
  - the deleted grant cache's tests are rewritten against rules.
- **API tests:**
  - create from a card resolves it and every matching in-scope pending card;
  - a pattern that does not cover the request is a 400;
  - a refusal is a 400, and a warning or global scope without confirm is a 409;
  - `via` of assistant, mcp-external, cli or internal is a 403;
  - legacy `"always"` maps to a session rule;
  - the profile CRUD refuses scoped profiles;
  - list, edit and revoke work.
- **Web tests:** the chooser flow, the keyboard, the confirm step, the Settings list, edit and
  revoke, and the full `npm run test`.

## 8. Rollout and kill switch

`VIBERPIT_PERMISSION_RULES_ENABLED` defaults to true. When false, the gate returns None and the rule
routes 404. The card then falls back to Allow/Deny.

## 9. Delivery

1. **PR 1: rule store and enforcement.** This spec, migration, `server/permission_rules/`, registry
   integration and grant-cache removal, the edge mapping and human-only guard, the API, the event
   field, the feature doc, and tests.
2. **PR 2: card UI.** The chooser, the preview, the keyboard and the phone sheet.
3. **PR 3: management.** The Settings section and the per-session view.
