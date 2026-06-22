# Structured tool-result & tool-input rendering

**Date:** 2026-06-22
**Status:** Approved (scope: full structured treatment)
**Area:** `web/src/components/session/` (transcript rendering) + `server/sessions/events.py` (tool classification)

## Problem

Session chat transcripts leak raw JSON. Every tool result is sent over the wire
as an envelope `payload = {"content": <tool output>}` (`server/sessions/translator.py:96`).
`content` is the Claude SDK tool-result content: usually a plain string,
sometimes a list of text blocks.

`web/src/components/session/ToolResult.tsx` only unwraps that envelope for four
kinds — `edit`/`write` (DiffView), `read` (FileContent), `bash` (BashResult),
`todo` (TodoList). Its `default` branch does `JSON.stringify(payload, null, 2)`,
dumping the **whole envelope** instead of extracting `.content`. So every
`search` (Grep/Glob/LS) and every `unknown` tool (WebFetch, WebSearch, Task,
Skill, every `mcp__*` tool, ExitPlanMode, …) renders as:

```json
{ "content": "src/foo.ts\nsrc/bar.ts\n…" }
```

These are among the most-used tools, so the leak fires constantly.

A secondary leak: the tool **args/input** panels render `JSON.stringify(block.input)`
verbatim (`ToolCallCard.tsx:89,128`).

## Goals

1. No raw envelope JSON in the default render path. `read`/`bash` already extract
   `.content`; that extract-and-strip behavior becomes the baseline for all kinds.
2. Tailored, readable renderers for the highest-traffic kinds (search, task, web, mcp).
3. A safe three-tier fallback (text → structured → raw) so nothing ever silently
   drops content, and a labeled raw-JSON escape hatch is always reachable but never
   the default view.
4. Clean, key-value rendering of tool inputs.

## Non-goals

- Bespoke renderers for individual MCP servers/tools (their names are open-ended).
  MCP results use the generic text/structured renderer with an MCP context header.
- Changing the wire format or the redaction pipeline.

## Architecture

### Classification stays single-source on the backend

`server/sessions/events.py` `_TOOL_KIND` remains the authority for categorization
(constitution principle VI). Expand `ToolKind` and the classifier:

- `WEB` — `WebFetch`, `WebSearch`
- `TASK` — `Task`
- `MCP` — any tool whose name starts with `mcp__` (prefix check in `tool_kind()`)
- existing: `EDIT`, `WRITE`, `READ`, `BASH`, `TODO`, `SEARCH`, `UNKNOWN`

The frontend `ToolKind` union (`web/src/session/events.ts`) gains `'web' | 'task' | 'mcp'`.
The tool **`name`** is already on `block.name`; thread it into `ToolResult` so
MCP/web renderers can show context (server·tool, URL, query).

### Rendering chain

`ToolResult.tsx` becomes a thin dispatcher:

| kind                | renderer         | shows |
|---------------------|------------------|-------|
| `edit`/`write`      | `DiffView`       | (unchanged) reconstructed diff |
| `read`              | `FileContent`    | (unchanged) file content |
| `bash`              | `BashResult`     | (unchanged) stdout/stderr |
| `todo`              | `TodoList`       | (unchanged) checklist |
| `search`            | `SearchResult`   | content lines as a list + summary ("N matches"/"N files") |
| `task`              | `SubagentResult` | subagent final text rendered as Markdown |
| `web`               | `WebResult`      | URL/query from `input` + content as Markdown/text |
| `mcp`               | `GenericResult`  | extracted text or structured view, MCP header |
| `unknown`/fallback  | `GenericResult`  | extracted text or structured view |

### `GenericResult` — the new safe default

Three-tier fallback; raw JSON is last resort only:

1. **Text** — `coerceText(payload.content)` + `stripAnsi`. If non-empty, render
   as text (monospace `<pre>`). Kills ~95% of leaks alone.
2. **Structured** — if the content (or, lacking content, the payload) is a JSON
   array of flat objects → compact table; a plain object → key-value list via
   `StructuredView`. Safe heuristic: only flat scalar values inline; nested
   objects/arrays are summarized (`{…}`, `[N]`) with the raw toggle for detail.
3. **Raw** — a collapsed `<details>` (`▸ raw`) containing
   `JSON.stringify(payload, null, 2)`. Rendered only when tiers 1 and 2 produce
   nothing; always available as a toggle otherwise. Preserves error (red) styling.

### Tool inputs

Replace `JSON.stringify(block.input, null, 2)` in both density branches of
`ToolCallCard` with `StructuredView` (key-value list) + a `▸ raw` toggle, so
inputs read cleanly. Long string values (e.g. Edit `new_string`) are shown
truncated with the raw toggle for full text.

### Component boundaries (new files, each one purpose, independently testable)

- `GenericResult.tsx` — text → structured → raw fallback chain.
- `SearchResult.tsx` — Grep/Glob/LS line list + count summary.
- `SubagentResult.tsx` — Task final text as Markdown.
- `WebResult.tsx` — WebFetch/WebSearch URL/query + content.
- `StructuredView.tsx` — shared key-value / table renderer with raw toggle.
  Reused by `GenericResult` and `ToolCallCard` (inputs).

`ToolResult.tsx` is reduced to a switch. `toolContent.ts` `coerceText`/`stripAnsi`
are reused unchanged.

## Error handling

- Malformed/empty payloads never throw: each renderer degrades to the next tier,
  ending at the raw escape hatch.
- Error results (`ok === false`) keep the red styling on whichever tier renders.
- `StructuredView` guards against circular refs / huge blobs by falling back to
  the raw toggle rather than attempting to inline them.

## Testing (TDD)

Frontend (`web`, vitest), new `__tests__`:
- `GenericResult`: string content; text-block-array content; JSON-array content → table;
  object content → key-value; empty/absent content → raw fallback present.
- `SearchResult`: multi-line content → list + "N matches"; empty → "no matches".
- `SubagentResult`: markdown content rendered (not escaped).
- `WebResult`: shows URL (WebFetch) / query (WebSearch) from input + body.
- `StructuredView`: flat object → rows; nested value summarized; raw toggle present.
- Regression in `ToolResult` test: `search`/`mcp`/`unknown` never emit the bare
  `{ "content": … }` envelope string.

Backend (`server`, pytest), extend `test_events.py`:
- `tool_kind("WebFetch")` / `("WebSearch")` → `WEB`
- `tool_kind("Task")` → `TASK`
- `tool_kind("mcp__github__list_prs")` → `MCP`
- existing mappings unchanged.

Gates: `vitest run`, `tsc --noEmit`, `eslint` (web); `pytest server/tests/` (server).
