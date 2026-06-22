# Clickable file-path references in the web transcript

**Date:** 2026-06-22
**Status:** Approved (design)
**Area:** `web/` frontend — session transcript rendering

## Problem

The Claude Code terminal renders `file_path:line_number` references as clickable
links that open the file in the user's editor. The ccswitch web transcript does
not: assistant text is rendered via `Markdown.tsx` (ReactMarkdown + remark-gfm),
which linkifies real markdown links and bare URLs but leaves file-path references
as inert text. Users reviewing a session in the web UI want those references to be
actionable, "as close to Claude Code's behavior as possible."

The terminal's "open in local editor" mechanism (OSC 8 hyperlinks / IDE extension)
has no meaning in a (possibly mobile) browser, so a faithful web emulation must
pick a web-native action.

## Decision

Clicking a file-path reference **copies the path string to the clipboard**.

This was chosen over the initially-considered "open on GitHub" target to keep the
first iteration purely frontend: no `Session.branch` plumbing, no git-ref
resolution, and no GitHub 404 risk from unpushed worktree branches. Opening on
GitHub remains a plausible future iteration; this design does not preclude it.

## Scope

- **In scope:** inline `code` spans inside finalized assistant transcript text and
  insight callouts — both render through `Markdown.tsx`.
- **Out of scope:** user bubbles (`UserBubble`, plain text), thinking blocks
  (`ThinkingBlock`), and the streaming-text path (`AssistantText` renders a live
  block as plain monospace with no markdown — clickability applies only once the
  block finalizes). The streaming limitation is accepted for this iteration.

## Detection — `isFilePathRef(text)`

A pure predicate decides whether an inline code span is a file reference. A span
qualifies when its text is a single token (contains no whitespace) **and** either:

- contains a `/` (e.g. `src/lib/bar.ts`, `./config.json`, `/etc/hosts`), or
- ends in a filename with an extension, optionally followed by `:line` or
  `:line:col` (e.g. `foo.py`, `App.tsx:12`).

Excludes whitespace-bearing spans (`npm install`), flags (`--force`), and bare
identifiers (`useState`). This keeps false positives near zero, matching the fact
that Claude almost always writes paths inside code spans.

The full span text is what gets copied, including any `:line[:col]` suffix.

## Components

1. **`web/src/components/session/filePathRef.ts`** — exports
   `isFilePathRef(text: string): boolean`. Pure, dependency-free, unit-tested.

2. **`web/src/components/session/CopyablePath.tsx`** — renders the clickable code
   element. Styled to match the existing inline `<code>` plus a pointer cursor and
   a subtle hover affordance. Manages a transient "Copied" state (~1.5s) after a
   successful copy.

3. **`web/src/components/session/Markdown.tsx`** — add a `code` entry to the
   ReactMarkdown `components` map: when the span is inline and
   `isFilePathRef(text)` is true, render `<CopyablePath text={...} />`; otherwise
   render the default `<code>`.

## Data flow

ReactMarkdown parses markdown → for each inline `code` node the custom renderer
runs `isFilePathRef` → path-like spans render as `CopyablePath` → `onClick` calls
`navigator.clipboard.writeText(text)` → on success sets `copied = true` → resets
after a timeout.

## Error handling

`navigator.clipboard` is unavailable in insecure contexts and some mobile
browsers. The copy is wrapped in try/catch; on failure it falls back to a
hidden-`<textarea>` + `document.execCommand('copy')`. "Copied" feedback shows only
on success. The handler never throws into render.

## Testing

- **`filePathRef.test.ts`** — table-driven positives (`web/src/foo.tsx:60`,
  `foo.py`, `./a/b.json`, `/etc/hosts`, `App.tsx:12:4`) and negatives
  (`npm install`, `useState`, `--force`, `1/2`, empty string).
- **`CopyablePath.test.tsx`** — mock `navigator.clipboard.writeText`; assert click
  copies the exact span text and surfaces "Copied"; assert the clipboard-failure
  path does not crash and falls back.
- **`Markdown` test** — a path-like code span renders as the clickable element; a
  non-path code span renders as a plain `<code>`.

## Known limitations / future work

- Live (streaming) responses remain non-clickable until the block finalizes.
- Copy-only — does not navigate. "Open on GitHub" (using `Session.repo` + a git
  ref) is a natural follow-up if copy proves insufficient.
