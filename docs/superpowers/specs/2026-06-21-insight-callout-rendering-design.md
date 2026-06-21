# Styled rendering for `★ Insight` callouts

**Date:** 2026-06-21
**Status:** Approved (design)
**Feature area:** Web transcript view (`web/src/components/session/`)

## Problem

When a Claude session runs in learning/explanatory output style, the assistant
emits callout blocks of the form:

```
★ Insight ─────────────────────────────────────
[2-3 educational points]
─────────────────────────────────────────────────
```

In the ccswitch transcript these flow through `AssistantText.tsx` as ordinary
markdown, so the box-drawing rule lines (`─`, U+2500) render as a wall of literal
dashes inline, visually indistinguishable from — and noisier than — normal
assistant prose. We want these blocks to render as a distinct, glanceable callout.

## Goal

Detect `★ Insight … ─────` blocks inside assistant prose and render them as a
styled accent-bar callout, while leaving all other prose untouched.

## Non-goals (YAGNI)

- Collapse/expand behavior (insights are **always expanded** — confirmed).
- Styling insights while a turn is still streaming.
- Any backend, transcript-schema, reducer, or block-type changes.
- Configurable/themable callout colors.

## Approach

**Render-layer split (chosen).** An insight is a *formatting convention within an
assistant text block*, not a distinct transcript event — the model writes it as
prose. So detection lives at the render layer, keeping it a pure presentation
concern with the smallest blast radius. The reducer, `Turn.tsx`, block types, and
backend are untouched.

Rejected alternatives:
- *Parse-layer block kind* (split in `reducer.ts` into a new `insight` block):
  leaks a formatting convention into the data model, touches the reducer + its
  test suite, and complicates streaming (block boundary mid-stream).
- *Custom remark/rehype plugin*: most markdown-correct but overkill for a fixed,
  simple delimiter pattern.

## Components / changes

All under `web/src/components/session/`.

### 1. `insights.ts` — pure parser (new)

```ts
export type Segment =
  | { kind: 'prose'; text: string }
  | { kind: 'insight'; text: string }   // body only, delimiters stripped

export function parseInsights(text: string): Segment[]
```

- Recognizes a block as: an opener line matching `^★\s*Insight\b.*─.*$` (a `★`,
  the word `Insight`, and at least one `─`), one or more body lines, then a
  closing line matching `^\s*─+\s*$`.
- Returns an ordered list of segments, preserving prose before/between/after
  insights. Supports multiple insights in one string.
- The `insight` segment's `text` is the **body only** (delimiter lines stripped),
  so it can be re-rendered as markdown.

**Fallbacks (never drop text):**
- No opener found → single `prose` segment containing the whole input (current
  behavior exactly).
- Opener with no matching closer → the opener and everything after it is emitted
  as `prose` (treated as not-an-insight rather than swallowed).

**Character correctness:** the delimiter is box-drawing `─` (U+2500), *not* ASCII
hyphen `-`. The regex matches `─`; a line of ASCII hyphens is **not** a delimiter.

### 2. `InsightCallout.tsx` (new)

- Visual style A (accent bar): left accent rule + tinted background, a `★ Insight`
  header, and the body rendered with the same `ReactMarkdown`
  (`remark-gfm` + `rehype-highlight`) used by `AssistantText`, so bullets/code
  inside an insight still format.
- Amber/gold accent (the ★ color) with explicit dark-mode class pairs; Tailwind
  utility classes consistent with `ThinkingBlock` (`rounded`, border, dark-mode
  pairs).

### 3. `AssistantText.tsx` (edit)

- `streaming === true`: unchanged — raw `whitespace-pre-wrap` (insights get
  styled only once the turn settles).
- `streaming === false`: call `parseInsights(text)` and map segments —
  `prose` → existing `ReactMarkdown`, `insight` → `InsightCallout`.

## Data flow

Unchanged upstream. The message stream → reducer → typed blocks → `Turn.tsx`
pipeline is identical; only the leaf rendering of a settled assistant `text`
block changes.

## Testing (TDD — constitution Principle V)

`insights.test.ts` (pure function):
- no-insight string → single `prose` passthrough
- single insight, no surrounding prose
- leading and trailing prose around an insight
- multiple insights interleaved with prose
- unterminated opener → emitted as prose, no text lost
- box-drawing `─` (U+2500) recognized; ASCII `-` line is **not** a delimiter

Component test (`__tests__/`):
- an insight string renders a callout: `★ Insight` header present, delimiter
  dashes not shown literally
- a plain assistant string renders no callout
