# Native Claude Code Slash Commands — Design

**Date:** 2026-06-22
**Feature area:** 001-ccswitch-web (web session manager)
**Status:** Approved design, pending implementation plan

## Problem

Users expect Claude Code slash commands (`/help`, `/clear`, `/model`, `/compact`,
custom project commands, skills) to work in the ccswitch web app. Today they do not:
the app runs sessions through the **`claude-agent-sdk`** (not a raw PTY / `claude` CLI
process), and the Composer sends prompt text directly to `query()` with no
preprocessing. A user typing `/clear` just sends the literal string `/clear` to the
model — no command executes, and there is no discovery/autocomplete.

The goal is a command system that mirrors the native Claude Code experience end to end:
**discovery + autocomplete UI** *and* **real execution semantics**.

### Architectural constraint that shapes everything

There is no harness layer interpreting slash commands in the prompt path — that layer
lives in the Claude Code CLI, which the SDK only partially exposes. So "native slash
command support" is **not a passthrough toggle**; it is a layer we design and own.

Key SDK facts (verified by code inspection of the installed `claude_agent_sdk`):

- **`ClaudeSDKClient.get_server_info()`** returns "available commands and output
  styles" — a per-session enumeration that already reflects the account's
  `CLAUDE_CONFIG_DIR` and the project `.claude/commands/`. **This is the discovery
  source of truth.** We do *not* scan markdown ourselves (would be a parallel
  implementation of what the SDK already owns — constitution VI).
- Execution is heterogeneous:
  - `set_model()`, `set_permission_mode()`, `interrupt()` are real SDK methods
    (the latter two already wrapped in `SessionDriver`).
  - `get_context_usage()` is read-only (tokens, autocompact threshold).
  - There is **no programmatic `/compact`**; whether literal `/compact` / `/clear`
    text does anything in stream-json mode is **uncertain and must be verified
    against the real SDK** (constitution III), not assumed.

## Goals

- `/`-triggered autocomplete in the Composer, populated from the live per-session
  command catalog (built-ins + custom commands + skills).
- Real execution for three command classes: **custom commands & skills**,
  **session control**, **client-only UI**.
- Single source of truth for *what commands exist* (the SDK), and a single
  server-side authority for *how each executes* (the dispatcher).

## Non-goals (v1, YAGNI)

- Account-rotation slash commands (`/switch`, `/accounts`) — explicitly deferred.
- Client-side argument parsing / flag DSL — args are one opaque string.
- Live file-watching of `.claude/commands/` — catalog refreshes on attach.
- A global `Cmd-K` palette — the trigger lives in the Composer, like the CLI.

## Approach (chosen: server-authoritative dispatcher)

Rejected alternatives:
- **Thin client passthrough** — send all `/text` to `query()`. Unreliable: interactive
  commands largely don't execute in stream-json mode; fails the "actually execute" goal.
- **Hybrid client classifier** — client hardcodes command kinds. Duplicates command
  knowledge across client/server; drifts from SDK truth (parallel implementation).

Chosen: the **server** owns a catalog and a dispatcher; the **frontend** is a thin
palette + result renderer. The server assigns each command a routing `kind`, so the
client never decides what a command *is* — only how to render what the server returns.
New custom commands flow through as `passthrough` with zero client changes.

## Components

### Server

1. **`CommandCatalog`** (`server/sessions/commands/catalog.py`)
   - Wraps `client.get_server_info()`; normalizes to
     `Command{name, description, argumentHint, kind}` where
     `kind ∈ {passthrough, control, client}`.
   - Cached per session; refreshed on attach.
   - **Filter, don't fake:** commands discovered but not actually executable
     (e.g. `/compact` if it no-ops, interactive-only `/login`, `/config`) are
     excluded so the palette never offers a silent no-op.

2. **`CommandDispatcher`** (`server/sessions/commands/dispatcher.py`)
   - Driver-owned (every command targets one session; the driver already holds the
     SDK client + permission/interrupt methods).
   - Input `{sessionId, name, args}` → look up in catalog → route by `kind` via a
     small strategy map → return "executed" or a client-render payload.
   - Calls `driver.set_model()`, `driver.set_permission_mode()`,
     `driver.reconnect()`, `driver._client.query()`, `client.get_context_usage()`.

### Frontend

3. **`CommandPalette`** (`web/src/components/session/CommandPalette.tsx`) — `/`-triggered
   autocomplete inside the existing `Composer`; fuzzy filter over the cached catalog;
   keyboard nav; argument hints; mobile bottom-sheet variant.

4. **Command result rendering** — overlays/cards for `client`-kind results
   (`/help`, `/context`); an **inline transcript marker** for `control` results
   ("Context cleared", "Model → sonnet").

## WebSocket protocol (3 new message types)

Catalog delivery (S→C), on attach + refresh:
```json
{ "type": "commands", "sessionId": "…",
  "commands": [
    { "name": "clear",   "description": "Clear conversation history", "argumentHint": null,     "kind": "control" },
    { "name": "model",   "description": "Switch model",               "argumentHint": "<model>", "kind": "control" },
    { "name": "help",    "description": "List commands",              "argumentHint": null,      "kind": "client" },
    { "name": "context", "description": "Show context usage",         "argumentHint": null,      "kind": "client" },
    { "name": "review",  "description": "Custom: review a PR",        "argumentHint": "<pr#>",   "kind": "passthrough" }
  ] }
```

Invocation (C→S):
```json
{ "type": "command", "sessionId": "…", "name": "model", "args": "sonnet" }
```

Result (S→C) — only for `client`-kind renders and errors:
```json
{ "type": "commandResult", "sessionId": "…", "name": "help",
  "ok": true, "render": "help", "data": { } }
```

`control` and `passthrough` outcomes surface through the **existing event stream** so
the transcript stays the single true record. `commandResult` is reserved for ephemeral
client-side renders (`/help`, `/context`) that must NOT pollute conversation history.

### Flow

- `attach` → server builds catalog via `get_server_info()` → emits `commands`.
- User types `/` → palette filters the cached catalog locally (no round-trip).
- Submit → bare known command → client sends `command`; otherwise stays a normal
  `prompt` (so `/`-leading text is never hijacked when it isn't a real command).
- Server dispatches: `control` → synthetic inline transcript event; `client` →
  `commandResult`; `passthrough` → `query()` streams normally.

## Per-command behavior (v1)

| Command | kind | Mechanism | Risk / fallback |
|---|---|---|---|
| `/help` | client | Render catalog as overlay | none — pure UI |
| `/context` | client | `get_context_usage()` → overlay | none — read-only |
| `/model [m]` | control | `driver.set_model(m)`; no arg → picker then `set_model` | low — wrap existing SDK method |
| permission-mode | control | existing `driver.set_permission_mode()` | none — already wired |
| `/clear` | control | `driver.reconnect()` (reconnect `resume=None`), clear transcript, inline marker | medium — clean reconnect preserving account/cwd/config-dir |
| `/compact` | control | **verify** `query("/compact")`; **fallback:** filter out of catalog if it no-ops | high — no programmatic API; **conditional ship** |
| custom commands & skills | passthrough | `driver._client.query("/name args")` → normal turn | medium — **verify** SDK expands `.claude/commands/*.md`; highest value |

Notes:
- `/clear` = reconnect with `resume=None`. Semantically correct: a conversation's
  history *is* the resumed session; dropping the resume handle is exactly "clear."
  Reuses existing `sdk_factory` resume threading.
- `/compact` ships **only if** the verification spike shows it works; otherwise deferred
  and filtered from the catalog.

## Error handling

- **Unknown command** (`/xyz` not in catalog): not sent; palette shows "no match"; on
  Enter falls through as a normal prompt.
- **Dispatch failure** (e.g. `set_model` rejects a name): `commandResult{ok:false,error}`
  → dismissible inline error; WS never crashes.
- **Catalog fetch failure** (`get_server_info()` throws): session works as today; palette
  degrades to "commands unavailable"; slash text passes through as prompts.
- **Stale catalog**: refreshed on every `attach`; mid-session additions appear after
  detach/reattach (acceptable v1).

## Palette UX

Triggered when `/` is the first character of an empty-ish Composer line; filters the
cached catalog locally.

```
┌─ Composer ────────────────────────────────┐
│ /mod▌                                       │
├────────────────────────────────────────────┤
│ ▸ /model        Switch model      <model>  │  ← selected (↑↓ to move)
│   /compact      Compact context            │
└────────────────────────────────────────────┘
  Enter run · Tab complete name · Esc dismiss
```

- `↑/↓` move; **`Tab`** completes the name and keeps editing (for arg commands);
  **`Enter`** runs the selected command; **`Esc`** closes (text stays).
- `argumentHint` shown greyed; persists after `Tab` until args typed.
- Grouping: built-ins first, then a `Custom` divider for `passthrough` commands.
- Loading: "loading commands…" until catalog arrives. Errors: dismissible inline.
- Mobile: bottom sheet, 44px touch targets.

## Testing (constitution V — TDD; III — verify against real SDK)

1. **Verification spike FIRST** (before UI): real-SDK probes for (a) custom
   `.claude/commands/*.md` expansion via `query()`, (b) `/compact` behavior →
   decides conditional ship, (c) `get_server_info()` shape. De-risks the table.
2. **Server unit tests**: catalog normalization + `kind` assignment;
   dispatcher routing per `kind`; filter-don't-fake exclusion; error→`commandResult`.
3. **Driver tests**: `set_model` wrapper; `reconnect` preserves account/cwd/config-dir.
4. **WS contract tests**: `command` in → correct outcome out, using the existing
   `CCSWITCH_DEV=1` / allowlist harness.
5. **Web tests (vitest)**: palette filtering/keyboard nav; passthrough-vs-prompt
   routing; `commandResult` rendering; loading/error states.

## Build order

1. Verification spike (gates `/compact` and the passthrough mechanism).
2. Server: `CommandCatalog` + `commands` emission on attach.
3. Server: `CommandDispatcher` + `command`/`commandResult` handlers; driver
   `set_model`/`reconnect` wrappers.
4. Frontend: catalog plumbing in `ws/client.ts`; `CommandPalette` in `Composer`.
5. Frontend: `commandResult` renders (`/help`, `/context`) + inline control markers.
6. Mobile bottom-sheet + polish.
