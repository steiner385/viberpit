# Clickable File-Path References Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make file-path references inside assistant-transcript code spans clickable so a click copies the path to the clipboard.

**Architecture:** A pure predicate (`isFilePathRef`) decides whether an inline code span is a file path. A small `CopyablePath` component renders those spans as a clickable `<code>` that copies its text and shows transient feedback. `Markdown.tsx`'s ReactMarkdown `components` map gets a `code` override that routes path-like inline spans to `CopyablePath` and leaves everything else as a plain `<code>`.

**Tech Stack:** React 18 + TypeScript, react-markdown v10 (+ remark-gfm, rehype-highlight), Vitest + @testing-library/react (jsdom), Tailwind.

## Global Constraints

- All new frontend files live under `web/src/components/session/`; tests under `web/src/components/session/__tests__/`.
- Tests run from `web/`: `npx vitest run <path>`. Type-check with `npx tsc --noEmit`; lint with `npx eslint <path>`.
- react-markdown is v10: the `code` component does **not** receive an `inline` prop. Detect inline code by `typeof children === 'string'` (fenced blocks arrive as highlighted element arrays, not strings).
- Preserve the existing `a` component override in `Markdown.tsx` (opens links in a new tab with `rel="noopener noreferrer"`).
- Drop the `node` prop before spreading onto DOM elements (existing pattern: `void node`).
- Commit messages end with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

### Task 1: `isFilePathRef` detection predicate

**Files:**
- Create: `web/src/components/session/filePathRef.ts`
- Test: `web/src/components/session/__tests__/filePathRef.test.ts`

**Interfaces:**
- Consumes: nothing.
- Produces: `export function isFilePathRef(text: string): boolean` — true when `text` is a single whitespace-free token that either contains `/` or ends in a `.<ext>` filename, optionally suffixed with `:line` or `:line:col`.

- [ ] **Step 1: Write the failing test**

```ts
// web/src/components/session/__tests__/filePathRef.test.ts
import { describe, it, expect } from 'vitest'
import { isFilePathRef } from '../filePathRef'

describe('isFilePathRef', () => {
  it.each([
    'web/src/foo.tsx:60',
    'web/src/foo.tsx',
    'foo.py',
    './config.json',
    '/etc/hosts',
    'src/lib/bar.ts',
    'App.tsx:12:4',
    '~/notes.md',
  ])('accepts path-like token %j', (s) => {
    expect(isFilePathRef(s)).toBe(true)
  })

  it.each([
    'npm install',        // whitespace
    'useState',           // bare identifier, no slash or extension
    '--force',            // flag
    '1/2',                // ratio: slash but no path-ish segment/extension
    '',                   // empty
    'a.b c.d',            // whitespace
    'https://example.com',// URL, not a file path
  ])('rejects non-path token %j', (s) => {
    expect(isFilePathRef(s)).toBe(false)
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run src/components/session/__tests__/filePathRef.test.ts`
Expected: FAIL — "Failed to resolve import '../filePathRef'" / `isFilePathRef is not a function`.

- [ ] **Step 3: Write minimal implementation**

```ts
// web/src/components/session/filePathRef.ts

// A code span is treated as a file-path reference when it is a single
// whitespace-free token that either contains a path separator or ends in a
// filename with an extension, optionally suffixed with :line or :line:col.
// Kept deliberately strict to avoid linkifying prose, flags, or URLs.
const LINE_SUFFIX = /(:\d+){0,2}$/

export function isFilePathRef(text: string): boolean {
  const token = text.trim()
  if (token === '' || /\s/.test(token)) return false
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(token)) return false // URL scheme (http://, etc.)

  const path = token.replace(LINE_SUFFIX, '')
  if (path === '') return false

  const last = path.slice(path.lastIndexOf('/') + 1)
  const hasExtension = /\.[A-Za-z0-9]+$/.test(last)
  const hasSeparator = path.includes('/') && /\/[^/]/.test(path) // a real segment after a slash

  return hasExtension || hasSeparator
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npx vitest run src/components/session/__tests__/filePathRef.test.ts`
Expected: PASS (all cases).

- [ ] **Step 5: Type-check, lint, commit**

```bash
cd web && npx tsc --noEmit && npx eslint src/components/session/filePathRef.ts src/components/session/__tests__/filePathRef.test.ts
git add web/src/components/session/filePathRef.ts web/src/components/session/__tests__/filePathRef.test.ts
git commit -m "feat(web): add isFilePathRef predicate for transcript code spans

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `CopyablePath` clickable code span

**Files:**
- Create: `web/src/components/session/CopyablePath.tsx`
- Test: `web/src/components/session/__tests__/CopyablePath.test.tsx`

**Interfaces:**
- Consumes: nothing (self-contained).
- Produces: `export function CopyablePath({ text }: { text: string }): JSX.Element` — renders a `<code role="button">` that copies `text` on click/Enter/Space, with `title` toggling from `"Copy path"` to `"Copied!"` for ~1.5s.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/session/__tests__/CopyablePath.test.tsx
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { CopyablePath } from '../CopyablePath'

describe('CopyablePath', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    Object.assign(navigator, {
      clipboard: { writeText: vi.fn().mockResolvedValue(undefined) },
    })
  })

  it('renders the path text inside a code element', () => {
    render(<CopyablePath text="web/src/foo.tsx:60" />)
    const el = screen.getByText('web/src/foo.tsx:60')
    expect(el.tagName).toBe('CODE')
    expect(el).toHaveAttribute('role', 'button')
    expect(el).toHaveAttribute('title', 'Copy path')
  })

  it('copies the text and shows feedback on click', async () => {
    render(<CopyablePath text="web/src/foo.tsx:60" />)
    fireEvent.click(screen.getByText('web/src/foo.tsx:60'))
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('web/src/foo.tsx:60')
    await waitFor(() =>
      expect(screen.getByText('web/src/foo.tsx:60')).toHaveAttribute('title', 'Copied!'),
    )
  })

  it('copies on Enter key', async () => {
    render(<CopyablePath text="foo.py" />)
    fireEvent.keyDown(screen.getByText('foo.py'), { key: 'Enter' })
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('foo.py')
  })

  it('does not throw when the clipboard write rejects', async () => {
    Object.assign(navigator, {
      clipboard: { writeText: vi.fn().mockRejectedValue(new Error('denied')) },
    })
    render(<CopyablePath text="foo.py" />)
    expect(() => fireEvent.click(screen.getByText('foo.py'))).not.toThrow()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run src/components/session/__tests__/CopyablePath.test.tsx`
Expected: FAIL — "Failed to resolve import '../CopyablePath'".

- [ ] **Step 3: Write minimal implementation**

```tsx
// web/src/components/session/CopyablePath.tsx
import { useEffect, useRef, useState } from 'react'

// Best-effort clipboard copy: prefer the async Clipboard API, fall back to a
// hidden textarea + execCommand for insecure contexts / older mobile browsers.
// Never throws; resolves to whether the copy succeeded.
async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // fall through to the legacy path
  }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    return ok
  } catch {
    return false
  }
}

interface Props { text: string }

// A transcript code span that looks like a file path. Clicking (or pressing
// Enter/Space) copies the path to the clipboard and flashes brief feedback.
export function CopyablePath({ text }: Props) {
  const [copied, setCopied] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => () => { if (timer.current) clearTimeout(timer.current) }, [])

  async function onCopy() {
    const ok = await copyText(text)
    if (!ok) return
    setCopied(true)
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => setCopied(false), 1500)
  }

  return (
    <code
      role="button"
      tabIndex={0}
      title={copied ? 'Copied!' : 'Copy path'}
      onClick={onCopy}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          void onCopy()
        }
      }}
      className="cursor-pointer rounded px-0.5 underline decoration-dotted underline-offset-2 hover:bg-gray-200/60 dark:hover:bg-gray-700/60"
    >
      {text}
    </code>
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npx vitest run src/components/session/__tests__/CopyablePath.test.tsx`
Expected: PASS (4 cases).

- [ ] **Step 5: Type-check, lint, commit**

```bash
cd web && npx tsc --noEmit && npx eslint src/components/session/CopyablePath.tsx src/components/session/__tests__/CopyablePath.test.tsx
git add web/src/components/session/CopyablePath.tsx web/src/components/session/__tests__/CopyablePath.test.tsx
git commit -m "feat(web): add CopyablePath clickable transcript code span

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Wire `CopyablePath` into the Markdown renderer

**Files:**
- Modify: `web/src/components/session/Markdown.tsx`
- Test: `web/src/components/session/__tests__/AssistantText.test.tsx` (extend the existing suite)

**Interfaces:**
- Consumes: `isFilePathRef` (Task 1), `CopyablePath` (Task 2).
- Produces: no new exports; `Markdown` now renders path-like inline code spans as `CopyablePath`.

- [ ] **Step 1: Write the failing test (append to the existing AssistantText suite)**

```tsx
// Append these two cases inside the existing `describe('AssistantText', ...)`
// block in web/src/components/session/__tests__/AssistantText.test.tsx

  it('renders a path-like inline code span as a clickable copy target', async () => {
    render(<AssistantText text={'see `web/src/foo.tsx:60` here'} streaming={false} />)
    const el = await screen.findByText('web/src/foo.tsx:60')
    expect(el.tagName).toBe('CODE')
    expect(el).toHaveAttribute('role', 'button')
    expect(el).toHaveAttribute('title', 'Copy path')
  })

  it('leaves a non-path inline code span as a plain code element', async () => {
    render(<AssistantText text={'run `npm install` now'} streaming={false} />)
    const el = await screen.findByText('npm install')
    expect(el.tagName).toBe('CODE')
    expect(el).not.toHaveAttribute('role')
  })
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run src/components/session/__tests__/AssistantText.test.tsx`
Expected: FAIL — the path span renders as a plain `<code>` (no `role`/`title` attribute) until the renderer is wired.

- [ ] **Step 3: Add the `code` override to Markdown.tsx**

Replace the entire file with:

```tsx
// web/src/components/session/Markdown.tsx
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import 'highlight.js/styles/github-dark.css'
import { isFilePathRef } from './filePathRef'
import { CopyablePath } from './CopyablePath'

interface Props { children: string }

// Shared markdown renderer for assistant transcript content (prose and insight
// callouts). Centralizes the plugin set, the safe-link policy, and clickable
// file-path code spans.
export function Markdown({ children }: Props) {
  return (
    <div className="markdown max-w-none text-gray-900 dark:text-gray-100">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={{
          // Open links in a new tab and sever the opener relationship so a
          // session link can't navigate the transcript tab away or reach
          // window.opener.
          a: ({ node, ...props }) => {
            void node // hast node — drop it rather than forward to the DOM
            return <a {...props} target="_blank" rel="noopener noreferrer" />
          },
          // Make path-like INLINE code spans clickable to copy. Fenced blocks
          // arrive as highlighted element arrays (not a string), so the
          // typeof guard restricts this to inline code only — no `inline`
          // prop exists in react-markdown v10.
          code: ({ node, children, ...props }) => {
            void node
            if (typeof children === 'string' && isFilePathRef(children)) {
              return <CopyablePath text={children} />
            }
            return <code {...props}>{children}</code>
          },
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  )
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `npx vitest run src/components/session/__tests__/AssistantText.test.tsx`
Expected: PASS — including the pre-existing cases (markdown links, insight callouts, code blocks) and the two new code-span cases.

- [ ] **Step 5: Full session-component test sweep + type-check + lint**

Run:
```bash
cd web && npx vitest run src/components/session && npx tsc --noEmit && npx eslint src/components/session
```
Expected: all PASS, no type errors, no lint errors. (Confirms the fenced-code-block test in AssistantText still renders a plain `<code>` — i.e. the typeof guard correctly excludes block code.)

- [ ] **Step 6: Commit**

```bash
git add web/src/components/session/Markdown.tsx web/src/components/session/__tests__/AssistantText.test.tsx
git commit -m "feat(web): clickable file-path code spans copy path to clipboard

Wire isFilePathRef + CopyablePath into the shared Markdown renderer so
path-like inline code spans in the transcript copy on click. Inline-only
via a typeof-string guard (react-markdown v10 has no inline prop).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage:**
- Decision (click copies path to clipboard) → Task 2 (`CopyablePath` + `copyText`). ✓
- Detection rule (single token, slash or extension, optional `:line[:col]`) → Task 1 (`isFilePathRef`). ✓
- Scope = assistant text + insight callouts via `Markdown.tsx`; streaming/user/thinking excluded → Task 3 wires only `Markdown.tsx`; streaming path in `AssistantText` is untouched (still plain monospace). ✓
- Components: `filePathRef.ts` (T1), `CopyablePath.tsx` (T2), `code` override in `Markdown.tsx` (T3). ✓
- Error handling: `navigator.clipboard` try/catch + hidden-textarea `execCommand` fallback, no throw into render, feedback only on success → Task 2 `copyText` + the reject-doesn't-throw test. ✓
- Testing: `filePathRef.test.ts` table (T1), `CopyablePath.test.tsx` incl. failure path (T2), Markdown path-vs-non-path cases (T3). ✓

**2. Placeholder scan:** No TBD/TODO/"handle edge cases"/"similar to". All steps carry full code and exact commands. ✓

**3. Type consistency:** `isFilePathRef(text: string): boolean` used identically in T1 and T3. `CopyablePath` prop is `{ text: string }` in T2 and called as `<CopyablePath text={children} />` in T3 (where `children` is narrowed to `string` by the `typeof` guard). `copyText(text: string): Promise<boolean>` is internal to T2. ✓
