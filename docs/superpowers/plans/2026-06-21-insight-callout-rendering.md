# Insight Callout Rendering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render `★ Insight … ─────` blocks inside assistant transcript text as a styled accent-bar callout instead of literal inline dashes.

**Architecture:** Pure render-layer detection. A new pure parser (`insights.ts`) splits a settled assistant string into ordered `prose`/`insight` segments; a new presentational component (`InsightCallout.tsx`) renders the insight segments; `AssistantText.tsx` maps segments to the right renderer. No reducer, block-type, transcript-schema, or backend changes.

**Tech Stack:** React + TypeScript + Vite, Tailwind, `react-markdown` (+ `remark-gfm`, `rehype-highlight`), Vitest + `@testing-library/react`.

## Global Constraints

- All work under `web/src/components/session/`. Run web commands from the `web/` directory.
- A fresh worktree has no built env: symlink `web/node_modules` from the main checkout before running tests (see CLAUDE.md Dev/testing).
- Test runner: `npx vitest run <path>` from `web/`. Type check: `npx tsc --noEmit`. Lint: `npx eslint <path>`.
- The insight delimiter character is box-drawing `─` (U+2500), **not** ASCII hyphen `-`. A line of ASCII hyphens is never a delimiter.
- Never drop assistant text: any unrecognized/malformed insight falls back to plain prose rendering.
- Component tests that mount a component importing the highlight CSS must mock it: `vi.mock('highlight.js/styles/github-dark.css', () => ({}))`.
- TDD per constitution Principle V: write the failing test first, watch it fail, then implement.

---

### Task 1: `parseInsights` pure parser

**Files:**
- Create: `web/src/components/session/insights.ts`
- Test: `web/src/components/session/__tests__/insights.test.ts`

**Interfaces:**
- Consumes: nothing (leaf utility).
- Produces:
  ```ts
  export type Segment =
    | { kind: 'prose'; text: string }
    | { kind: 'insight'; text: string }   // body only, delimiter lines stripped
  export function parseInsights(text: string): Segment[]
  ```
  Behavior: returns ordered segments preserving prose before/between/after insights. No insight present → exactly one `prose` segment equal to the whole input. An opener (`★ Insight …─…`) with no following closer line is emitted as prose (no text lost). Multiple insights supported.

- [ ] **Step 1: Write the failing test**

Create `web/src/components/session/__tests__/insights.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { parseInsights } from '../insights'

const OPEN = '★ Insight ─────────────────────────────────────'
const CLOSE = '─────────────────────────────────────────────────'

describe('parseInsights', () => {
  it('returns whole input as a single prose segment when no insight present', () => {
    expect(parseInsights('hello **world**')).toEqual([
      { kind: 'prose', text: 'hello **world**' },
    ])
  })

  it('extracts a lone insight, stripping delimiter lines', () => {
    const input = [OPEN, 'point one', 'point two', CLOSE].join('\n')
    expect(parseInsights(input)).toEqual([
      { kind: 'insight', text: 'point one\npoint two' },
    ])
  })

  it('preserves prose before and after an insight, in order', () => {
    const input = ['before', OPEN, 'body', CLOSE, 'after'].join('\n')
    expect(parseInsights(input)).toEqual([
      { kind: 'prose', text: 'before' },
      { kind: 'insight', text: 'body' },
      { kind: 'prose', text: 'after' },
    ])
  })

  it('handles multiple insights interleaved with prose', () => {
    const input = [OPEN, 'a', CLOSE, 'mid', OPEN, 'b', CLOSE].join('\n')
    expect(parseInsights(input)).toEqual([
      { kind: 'insight', text: 'a' },
      { kind: 'prose', text: 'mid' },
      { kind: 'insight', text: 'b' },
    ])
  })

  it('treats an unterminated opener as prose and never drops text', () => {
    const input = [OPEN, 'dangling body', 'more text'].join('\n')
    const result = parseInsights(input)
    expect(result).toEqual([{ kind: 'prose', text: input }])
  })

  it('does not treat a line of ASCII hyphens as a delimiter', () => {
    const input = ['★ Insight ----------', 'body', '----------'].join('\n')
    expect(parseInsights(input)).toEqual([{ kind: 'prose', text: input }])
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx vitest run src/components/session/__tests__/insights.test.ts`
Expected: FAIL — cannot resolve `../insights` / `parseInsights is not a function`.

- [ ] **Step 3: Write minimal implementation**

Create `web/src/components/session/insights.ts`:

```ts
export type Segment =
  | { kind: 'prose'; text: string }
  | { kind: 'insight'; text: string }

// Opener: a line containing ★, the word Insight, and at least one box-drawing ─.
const OPENER = /^\s*★\s*Insight\b[^\n]*─[^\n]*$/
// Closer: a line of only box-drawing ─ (plus optional surrounding whitespace).
const CLOSER = /^\s*─+\s*$/

export function parseInsights(text: string): Segment[] {
  const lines = text.split('\n')
  const segments: Segment[] = []
  let prose: string[] = []

  const flushProse = () => {
    if (prose.length > 0) {
      segments.push({ kind: 'prose', text: prose.join('\n') })
      prose = []
    }
  }

  for (let i = 0; i < lines.length; i++) {
    if (OPENER.test(lines[i])) {
      let j = i + 1
      while (j < lines.length && !CLOSER.test(lines[j])) j++
      if (j < lines.length) {
        flushProse()
        segments.push({ kind: 'insight', text: lines.slice(i + 1, j).join('\n').trim() })
        i = j // skip past the closing delimiter line
        continue
      }
      // No closer found: fall through and treat the opener line as plain prose.
    }
    prose.push(lines[i])
  }
  flushProse()
  return segments
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && npx vitest run src/components/session/__tests__/insights.test.ts`
Expected: PASS (6 passing).

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/insights.ts web/src/components/session/__tests__/insights.test.ts
git commit -m "feat(sessions): parseInsights — split assistant text into prose/insight segments

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `InsightCallout` component

**Files:**
- Create: `web/src/components/session/InsightCallout.tsx`
- Test: `web/src/components/session/__tests__/InsightCallout.test.tsx`

**Interfaces:**
- Consumes: nothing from earlier tasks (renders a body string).
- Produces: `export function InsightCallout({ text }: { text: string }): JSX.Element` — a Style-A accent-bar callout with a `★ Insight` header and the body rendered as markdown.

- [ ] **Step 1: Write the failing test**

Create `web/src/components/session/__tests__/InsightCallout.test.tsx`:

```tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { InsightCallout } from '../InsightCallout'

vi.mock('highlight.js/styles/github-dark.css', () => ({}))

describe('InsightCallout', () => {
  it('renders the ★ Insight header', () => {
    render(<InsightCallout text="a point" />)
    expect(screen.getByText('Insight')).toBeInTheDocument()
  })

  it('renders the body as markdown', async () => {
    render(<InsightCallout text="**bold** point" />)
    const strong = await screen.findByText('bold')
    expect(strong.tagName).toBe('STRONG')
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx vitest run src/components/session/__tests__/InsightCallout.test.tsx`
Expected: FAIL — cannot resolve `../InsightCallout`.

- [ ] **Step 3: Write minimal implementation**

Create `web/src/components/session/InsightCallout.tsx`:

```tsx
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import 'highlight.js/styles/github-dark.css'

interface Props { text: string }

export function InsightCallout({ text }: Props) {
  return (
    <div className="my-3 rounded-r border-l-[3px] border-amber-400 bg-amber-50 px-3.5 py-3 dark:bg-amber-400/10">
      <div className="mb-1.5 flex items-center gap-1.5 text-[13px] font-semibold text-amber-700 dark:text-amber-300">
        <span aria-hidden="true">★</span> Insight
      </div>
      <div className="prose prose-sm dark:prose-invert max-w-none">
        <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
          {text}
        </ReactMarkdown>
      </div>
    </div>
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && npx vitest run src/components/session/__tests__/InsightCallout.test.tsx`
Expected: PASS (2 passing).

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/InsightCallout.tsx web/src/components/session/__tests__/InsightCallout.test.tsx
git commit -m "feat(sessions): InsightCallout — accent-bar callout for ★ Insight blocks

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Wire insights into `AssistantText`

**Files:**
- Modify: `web/src/components/session/AssistantText.tsx`
- Test: `web/src/components/session/__tests__/AssistantText.test.tsx` (add cases)

**Interfaces:**
- Consumes: `parseInsights` (Task 1), `InsightCallout` (Task 2).
- Produces: no new exports — same `AssistantText({ text, streaming })` signature. Settled (`streaming === false`) text is segmented; insight segments render as `InsightCallout`, prose segments render via `ReactMarkdown` exactly as before. Streaming path unchanged.

- [ ] **Step 1: Write the failing test**

Add to `web/src/components/session/__tests__/AssistantText.test.tsx` inside the existing `describe('AssistantText', …)` block:

```tsx
  it('renders an embedded ★ Insight block as a callout, not literal dashes', async () => {
    const text = [
      'intro line',
      '★ Insight ─────────────────────────────────────',
      'a key point',
      '─────────────────────────────────────────────────',
    ].join('\n')
    render(<AssistantText text={text} streaming={false} />)
    expect(await screen.findByText('Insight')).toBeInTheDocument()
    expect(await screen.findByText('a key point')).toBeInTheDocument()
    // The box-drawing rule lines are not rendered as visible content.
    expect(screen.queryByText(/─────/)).toBeNull()
  })

  it('renders plain settled text without an insight callout', () => {
    render(<AssistantText text="just prose" streaming={false} />)
    expect(screen.getByText('just prose')).toBeInTheDocument()
    expect(screen.queryByText('Insight')).toBeNull()
  })
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx vitest run src/components/session/__tests__/AssistantText.test.tsx`
Expected: FAIL — the insight case finds no `Insight` header (current code renders the raw markdown, so `─────` text is present).

- [ ] **Step 3: Write minimal implementation**

Replace the contents of `web/src/components/session/AssistantText.tsx` with:

```tsx
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import 'highlight.js/styles/github-dark.css'
import { parseInsights } from './insights'
import { InsightCallout } from './InsightCallout'

interface Props { text: string; streaming: boolean }

export function AssistantText({ text, streaming }: Props) {
  if (streaming) {
    return (
      <div className="whitespace-pre-wrap text-sm text-gray-900 dark:text-gray-100 font-mono">
        {text}
      </div>
    )
  }

  const segments = parseInsights(text)
  return (
    <>
      {segments.map((seg, i) =>
        seg.kind === 'insight' ? (
          <InsightCallout key={i} text={seg.text} />
        ) : (
          <div key={i} className="prose prose-sm dark:prose-invert max-w-none">
            <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
              {seg.text}
            </ReactMarkdown>
          </div>
        ),
      )}
    </>
  )
}
```

- [ ] **Step 4: Run the full session suite + type check**

Run: `cd web && npx vitest run src/components/session/__tests__/AssistantText.test.tsx src/components/session/__tests__/InsightCallout.test.tsx src/components/session/__tests__/insights.test.ts`
Expected: PASS (all, including the pre-existing AssistantText streaming/markdown cases — the no-insight path is unchanged).

Run: `cd web && npx tsc --noEmit && npx eslint src/components/session/insights.ts src/components/session/InsightCallout.tsx src/components/session/AssistantText.tsx`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/AssistantText.tsx web/src/components/session/__tests__/AssistantText.test.tsx
git commit -m "feat(sessions): render ★ Insight callouts in settled assistant text

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- `insights.ts` pure parser + fallbacks → Task 1 (incl. unterminated-opener and ASCII-hyphen cases).
- `InsightCallout.tsx` Style-A accent bar, markdown body → Task 2.
- `AssistantText.tsx` segment mapping, streaming path unchanged → Task 3.
- Testing requirements (passthrough, single, leading/trailing, multiple, unterminated, U+2500 vs ASCII; component header + no literal dashes) → Tasks 1 & 3 tests.
- Non-goals (no collapse, no streaming styling, no backend/reducer change) → respected: reducer/`Turn.tsx`/types/backend untouched; streaming branch unchanged.

**Placeholder scan:** none — every code/test step shows full content.

**Type consistency:** `Segment`/`parseInsights` signature defined in Task 1 is consumed verbatim in Task 3; `InsightCallout({ text })` defined in Task 2 is consumed verbatim in Task 3.
