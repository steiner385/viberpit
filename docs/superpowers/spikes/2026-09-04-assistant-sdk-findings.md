# Assistant SDK spike — findings (2026-09-04)

Binary: `claude 2.1.259 (Claude Code)` (system CLI at `/home/tony/.local/bin/claude`) ·
SDK: `claude-agent-sdk` 0.2.105 · account: `tony.claude_steinonline.net`

## Summary

The mandated script (`scripts/spike_assistant_sdk.py`, matching the brief's Step 1 code plus
the two required fixes below) **hangs indefinitely** at `ClaudeSDKClient.__aenter__` and never
emits a `SystemMessage`, because it passes `setting_sources=[]` to the **persistent
`ClaudeSDKClient`**. This is reproducible 100% of the time across 5+ isolated runs (see Q3 and
"Extra finding: `setting_sources` hangs `ClaudeSDKClient`" below) — this spike only exercised
`ClaudeSDKClient`, never the one-shot `claude_agent_sdk.query()` function, and the two paths are
not interchangeable here (see that section for the working `query()` counter-example already in
production).
To still answer Q1/Q2/Q4/Q5 with real evidence, I ran an option-for-option identical probe with
only `setting_sources` omitted (`/tmp/bisect7_full_probe.py`, logged to
`/tmp/spike-full-probe.log`) — every other option (`tools=[]`, `disallowed_tools`, `mcp_servers`,
`strict_mcp_config=True`, `can_use_tool`, `permission_mode`, `max_turns`, `effort`, `model`,
`include_partial_messages`) is unchanged from the brief. Q3 is reported BLOCKED on its own terms;
Q1/Q2/Q4/Q5 are answered from the near-identical run, called out explicitly wherever that matters.

## Required fixes to the mandated script (both applied, both are findings)

1. **`"MultiEdit"` removed from `disallowed_tools`** — a prior interrupted attempt's first line of
   output was `Permission deny rule "MultiEdit" matches no known tool — check for typos.` MultiEdit
   is not a tool on CLI 2.1.259. **Consequence:** Task 1.5's `DISALLOWED_BUILTINS` must not include
   `MultiEdit`.
2. **`cli_path=shutil.which("claude")` added** — without it, the SDK silently spawns its own
   vendored `claude_agent_sdk/_bundled/claude`, confirmed via `ps -ef --forest` to be **2.1.183**,
   not the system CLI (2.1.259) that `CLAUDE_CONFIG_DIR` and the rest of the environment were set
   up for. This is the same shadow pattern already tracked in project memory
   (`sdk-bundled-cli-shadows-system-claude`, fixed for production via `claude_bin.py`). The bundled
   CLI also behaved differently (busy-looping at high CPU rather than idle-sleeping).
   **Consequence:** any Phase 1+ code that spawns `ClaudeSDKClient` directly (not via ccswitch's
   existing `claude_bin` resolution) must set `cli_path` explicitly — the SDK does not respect
   `PATH` on its own despite `claude` being importable from it.

## Extra finding: `setting_sources` (any value) hangs `ClaudeSDKClient` at init

**Scope: this is a `ClaudeSDKClient`-only finding.** Everything below was tested exclusively
through the persistent `ClaudeSDKClient` (the `can_use_tool`-driven, multi-turn client this spike
exists to evaluate). It does **not** generalize to the one-shot `claude_agent_sdk.query()`
function — three production helpers already pass `setting_sources=[]` to `query()` and run daily
without hanging: `server/sessions/prompt_refiner.py:73`, `server/sessions/title_generator.py:115`,
and `server/automations/model_pick.py:113` (all three also pass `env=helper_env(config_dir)` — a
scratch, per-call `CLAUDE_CONFIG_DIR` — rather than reusing the caller's live account config
directly, which may be why the `query()` path doesn't hit whatever `ClaudeSDKClient` hits).

Isolated by bisection (all runs against the same account, `timeout`-bounded, `python -u`):

| Options | Result |
|---|---|
| bare (only `cli_path`, `max_turns=1`) | `[connected] 1.60s` → `[cold-start] 1.60s subtype=hook_started` |
| `system_prompt` only | `[connected] 1.57s` → `[cold-start] 1.57s subtype=hook_started` |
| `setting_sources=[]` only | `[connected] 0.80s` → **no further output, timeout at 45s** (reproduced 3×, ~0.8–1.6s connect then silence to 30–90s timeouts) |
| `setting_sources=["project"]` only | `[connected] 1.17s` → **no further output, timeout at 30s** |
| full brief option set, `setting_sources` **omitted** | `[cold-start] 1.90–2.18s` → completes all 4 prompts normally (see below) |

`connect()` (the `ClaudeSDKClient.__aenter__` / control-channel handshake) succeeds and returns
before any `setting_sources` value is even sent — the hang is inside `receive_messages()` waiting
for the first `SystemMessage`, which the CLI subprocess never emits once `--setting-sources` (any
value, including empty) is on its argv. `/proc/<pid>/wchan` for the CLI child showed `ep_poll`
(idle, not busy) the whole time — it is not crashing or spinning, it simply never completes
initialization. **This is present with the *system* CLI (2.1.259), not just the bundled one.**

**Consequence:** Task 1.5 must not pass `setting_sources` on the persistent-`ClaudeSDKClient` path
— it hangs every session with SDK 0.2.105 + CLI 2.1.259, not just misbehaves. Do not treat this as
an SDK/CLI-version blocker for Phase 1: do not pass `setting_sources` on the persistent-client
path; isolate via the scratch `CLAUDE_CONFIG_DIR` from `helper_env()` instead
(`server/sessions/account_auth.py:50`) — the pattern all three `query()` call sites above already
use in production. This spike did not verify that `helper_env()`'s scratch dir on its own
suppresses `~/.claude/CLAUDE.md`/plugin loading the way `setting_sources=[]` was intended to (it is
a host-local, torn-read-safe *mirror* of the account's own config dir per `server/sessions/
scratch_config.py`, not an empty one) — Task 1.5 should confirm that detail when it designs the
assistant's config-dir isolation, but should build on `helper_env()`'s established pattern rather
than reaching for `setting_sources` on `ClaudeSDKClient`.

## Q1–Q5

| Q | Result | Evidence (log excerpt) |
|---|---|---|
| Q1 — do built-ins survive `tools=[]`+`disallowed_tools`, and does the model call `Bash`? | **No built-ins exposed in `server_info` at all** (there is no `tools` key in this SDK/CLI's `get_server_info()` — see below); **model does NOT call Bash**, it explicitly refuses. | `[server_info.KEYS] ['commands', 'agents', 'output_style', 'available_output_styles', 'models', 'account', 'pid', 'current_permission_mode', 'analytics_disabled', 'remote_control_auto_enable', 'remote_control_available', 'remote_control_auto_on_by_default', 'ide_rc_auto_enable_gate', 'fast_mode_state', 'fast_mode_disabled_reason', 'session_state']` (no `tools` key) — and — `[assistant] I can't do that — my toolset here is restricted to ccswitch tools only (list_sessions and archive_session). I don't have Bash or filesystem access in this session, so I can't run ls -la.` — and `[seen tool calls] [('mcp__ccswitch__list_sessions', {}), ('mcp__ccswitch__archive_session', {'id': 'abc'})]` (no Bash entry ever) |
| Q2 — does `can_use_tool` fire for `mcp__ccswitch__*`? | **Yes.** | `[can_use_tool] mcp__ccswitch__list_sessions {}` and `[can_use_tool] mcp__ccswitch__archive_session {"id": "abc"}` |
| Q3 — does custom `system_prompt` + `setting_sources=[]` skip user `CLAUDE.md`/plugins, on `ClaudeSDKClient`? | **BLOCKED for `ClaudeSDKClient`** — `setting_sources=[]` hangs that client indefinitely (see above); cannot be tested as specified. Scoped to `ClaudeSDKClient` only — this was never tested against `claude_agent_sdk.query()`, where `setting_sources=[]` already runs daily in production (`server/sessions/prompt_refiner.py:73`, `server/sessions/title_generator.py:115`, `server/automations/model_pick.py:113`). As a negative control on `ClaudeSDKClient`, the *default* setting sources (no `setting_sources` kwarg at all — i.e. plugins/CLAUDE.md load normally) were confirmed to load the real `~/.claude/CLAUDE.md`: the model quoted its actual first line correctly. This proves the loading path is real and observable, but not that `setting_sources=[]` would suppress it on this client — that combination never reaches a testable state. | `[assistant] Your global /home/tony/.claude/CLAUDE.md begins with this first line: > \`## Time and Timestamps\`` (from the run WITHOUT `setting_sources`) — vs. `[cli_path] /home/tony/.local/bin/claude` then silence to timeout (the actual committed script, WITH `setting_sources=[]`, `/tmp/spike-assistant-sdk.log`) |
| Q4 — does `strict_mcp_config=True` hide the account's configured MCP servers? | **Yes, by strong behavioral evidence** — `server_info` has no `mcp_servers` key in this SDK/CLI version either (can't confirm via the field the brief expected), but the account has 10 configured plugin MCP servers (github, linear, sentry, chrome-devtools, stripe, railway, desktop-commander, twilio-docs, aws-mcp, notion — several `✔ Connected`), and across the whole 4-prompt run **zero** non-`ccswitch` tool calls or references occurred; the model self-reported its toolset as restricted to `list_sessions`/`archive_session` only. | `claude mcp list` (same account): `plugin:chrome-devtools-mcp:chrome-devtools: ... - ✔ Connected`, `plugin:desktop-commander:desktop-commander: ... - ✔ Connected`, `plugin:twilio-developer-kit:twilio-docs: ... - ✔ Connected`, `plugin:aws-core:aws-mcp: ... - ✔ Connected` (4 healthy, non-ccswitch servers configured) — none appeared in `[seen tool calls]` or `can_use_tool` output during the probe |
| Q5 — cold start / first delta | cold start **1.90–2.18s** (`ClaudeSDKClient.__aenter__` → first `SystemMessage`); first `TextDelta` for a one-tool question **1.12–1.39s** into that turn (total turn 4.3–7.6s) | `[cold-start] 1.90s subtype=hook_started` — `[result] turns=2 first_delta=1.39s total=7.60s` (first prompt) |

## Log excerpts

Committed script (`scripts/spike_assistant_sdk.py`, exactly as specified minus `MultiEdit` +
`cli_path`), `/tmp/spike-assistant-sdk.log`, final run:
```
[cli_path] /home/tony/.local/bin/claude
```
(then silence to the 360s `timeout` kill — `PIPE_EXIT=124`). Reproduced this exact hang 5 times
(two 360s-bounded, three 90s-bounded) plus 2 additional runs where a concurrent, unrelated sshfs
torn-read on the shared `.claude.json` (see "Unrelated environmental noise" below) produced a fast
crash instead of a silent hang — same root cause (`setting_sources=[]`) confirmed independently via
bisection once the config-file race was out of the picture.

Bisection probe (`/tmp/bisect7_full_probe.py` — full brief option set, `setting_sources` omitted),
`/tmp/spike-full-probe.log`, full run:
```
[cli_path] /home/tony/.local/bin/claude
[cold-start] 1.90s subtype=hook_started
[server_info.KEYS] ['commands', 'agents', 'output_style', 'available_output_styles', 'models', 'account', 'pid', 'current_permission_mode', 'analytics_disabled', 'remote_control_auto_enable', 'remote_control_available', 'remote_control_auto_on_by_default', 'ide_rc_auto_enable_gate', 'fast_mode_state', 'fast_mode_disabled_reason', 'session_state']
[server_info.tools] null
[server_info.mcp] null
[can_use_tool] mcp__ccswitch__list_sessions {}
[assistant] You have **1 session** that needs attention: ...
[result] turns=2 first_delta=1.39s total=7.60s
[assistant] I can't do that — my toolset here is restricted to ccswitch tools only ...
[result] turns=1 first_delta=1.17s total=4.28s
[can_use_tool] mcp__ccswitch__archive_session {"id": "abc"}
[assistant] Done — session abc ("Refactor auth middleware") has been archived successfully. ...
[result] turns=2 first_delta=1.12s total=5.11s
[assistant] Your global /home/tony/.claude/CLAUDE.md begins with this first line: > `## Time and Timestamps` ...
[result] turns=1 first_delta=1.16s total=4.55s
[seen tool calls] [('mcp__ccswitch__list_sessions', {}), ('mcp__ccswitch__archive_session', {'id': 'abc'})]
```

Additional verified-by-controller fact (recorded here per brief instructions):
`claude_agent_sdk.tool(name, description, input_schema, annotations: mcp.types.ToolAnnotations |
None = None)` — the `annotations` kwarg exists on SDK 0.2.105, so Task 1.4 can use it.

## Unrelated environmental noise (not an assistant-SDK finding)

While bisecting, two runs hit the pre-existing, already-documented `sshfs-shared-config-torn-reads`
issue on this host (rocky): `.claude.json` under the shared `CLAUDE_CONFIG_DIR` was concurrently
rewritten by another host and read mid-write, producing `JSON Parse error: Unterminated string` /
`Property name must be a string literal`. The CLI self-heals (backs up the corrupted file) but the
process then still needed a clean retry. This is orthogonal to the `setting_sources` hang — it was
reproduced independently with a stable, valid config file — and does not need separate action here
beyond noting it recurred.

## Consequences for the plan

- Task 1.5's `DISALLOWED_BUILTINS` must not include `"MultiEdit"` — it isn't a tool on CLI 2.1.259
  and the CLI logs a permission-rule warning for it.
- Any code that constructs `ClaudeAgentOptions`/`ClaudeSDKClient` directly (not through ccswitch's
  existing `claude_bin` resolution) **must** pass `cli_path` explicitly, or it silently runs the
  SDK's older vendored CLI instead of the account's real one.
- **For Task 1.5:** `setting_sources` (any value) hangs `ClaudeSDKClient` outright with SDK 0.2.105
  + CLI 2.1.259 — this was only tested on `ClaudeSDKClient`, not on `claude_agent_sdk.query()`,
  where `setting_sources=[]` already runs in production without hanging
  (`server/sessions/prompt_refiner.py:73`, `server/sessions/title_generator.py:115`,
  `server/automations/model_pick.py:113`). Do not pass `setting_sources` on the persistent-client
  path; isolate via the scratch `CLAUDE_CONFIG_DIR` from `helper_env()`
  (`server/sessions/account_auth.py:50`) instead — not "needs a design change or an SDK/CLI version
  bump."
- `get_server_info()` in this SDK/CLI version has no `tools` or `mcp_servers` key — anything in
  Tasks 1.5/3.2 that meant to introspect available tools or configured MCP servers via
  `get_server_info()` needs a different signal (e.g. `can_use_tool` call observation, as used here)
  since the field the plan expected does not exist on the wire.
- If `tools=[]` + `disallowed_tools` genuinely keeps `Bash` etc. out of reach (consistent with the
  observed refusal and empty `seen tool calls`), Task 3.2's `_deny_non_assistant_tools` is
  belt-and-braces, not load-bearing — but re-confirm once Q3's blocker is resolved and a full
  brief-exact run can complete, since we never got `server_info.tools` data to double-check this
  independently of behavior.
- `strict_mcp_config=True` behaviorally hid all 10 of the account's configured plugin MCP servers
  in this run — supports Task 1.5's assumption that the assistant session won't accidentally expose
  Notion/GitHub/Stripe/etc. tools, but note the confirmation is behavioral (no non-ccswitch tool
  call happened), not structural (no `server_info.mcp_servers` field to assert against).
