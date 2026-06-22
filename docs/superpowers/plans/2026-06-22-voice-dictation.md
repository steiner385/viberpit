# Voice Dictation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users dictate prompts by voice on both the new-session launcher (`DispatchBar`) and the per-session prompt box (`Composer`), using the browser Web Speech API, with transcript inserted at the cursor for review before sending.

**Architecture:** Two pure helpers (`spliceAt`, `autoGrow`) + a low-level Web Speech wrapper hook (`useDictation`) + a composing hook that inserts transcript into a controlled textarea at the caret (`useDictationField`) + a presentational `MicButton` that renders nothing when speech is unsupported. Both input surfaces call `useDictationField` and render `<MicButton/>`. The session engine and WebSocket `prompt()` path are untouched — voice merely produces the `text` string that already flows into them.

**Tech Stack:** React 19 + TypeScript, Tailwind, `lucide-react` (Mic icon), Vitest + `@testing-library/react` (`renderHook`, `vi.stubGlobal`).

## Global Constraints

- All work is frontend-only under `web/`. No backend, no new dependencies.
- No audio leaves the device; only the browser's built-in Web Speech API is used.
- Graceful degradation: where `SpeechRecognition`/`webkitSpeechRecognition` is absent (e.g. iOS Safari), the mic affordance must not render — feature-detect, never UA-sniff.
- Language hardcoded to `'en-US'` for v1.
- No auto-send: dictation only fills the textarea; the user reviews and presses Enter.
- Insert transcript **at the caret**, appending; interim results show live and are replaced by the final text.
- Tailwind touch-target convention in this codebase: interactive controls use `min-h-[44px]` and `focus-visible:ring-2`.
- Test command (run in `web/`): `npx vitest run`, `npx tsc --noEmit`, `npx eslint <files>`.
- A fresh worktree has no `node_modules`; symlink it from the main checkout first:
  `ln -s /mnt/ssk-ssd/tony/GitHub/cc-account-switcher/web/node_modules web/node_modules` (only if `web/node_modules` is missing).

---

### Task 1: Pure helpers — `spliceAt` and `autoGrow`

**Files:**
- Create: `web/src/lib/dictationInsert.ts`
- Create: `web/src/lib/autoGrow.ts`
- Test: `web/src/lib/__tests__/dictationInsert.test.ts`
- Test: `web/src/lib/__tests__/autoGrow.test.ts`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `spliceAt(current: string, anchor: number, removeLen: number, insert: string): string` — returns `current` with `removeLen` chars removed at `anchor` and `insert` spliced in.
  - `autoGrow(el: HTMLTextAreaElement | null, maxPx?: number): void` — resets height to `auto` then sets it to `min(scrollHeight, maxPx)` (default 192); no-op when `el` is null.

- [ ] **Step 1: Write the failing test for `spliceAt`**

Create `web/src/lib/__tests__/dictationInsert.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { spliceAt } from '../dictationInsert'

describe('spliceAt', () => {
  it('inserts at the anchor when nothing is removed', () => {
    expect(spliceAt('hi world', 2, 0, ' there')).toBe('hi there world')
  })

  it('replaces a previously-inserted interim chunk', () => {
    // "hi |world" with a 5-char interim "hello" already at anchor 3 → replace with "hello world"
    expect(spliceAt('hi hello', 3, 5, 'hello world')).toBe('hi hello world')
  })

  it('inserts at the end when anchor is at length', () => {
    expect(spliceAt('done', 4, 0, ' deal')).toBe('done deal')
  })

  it('treats anchor 0 with no removal as a prepend', () => {
    expect(spliceAt('world', 0, 0, 'hello ')).toBe('hello world')
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run (from `web/`): `npx vitest run src/lib/__tests__/dictationInsert.test.ts`
Expected: FAIL — `Failed to resolve import "../dictationInsert"`.

- [ ] **Step 3: Implement `spliceAt`**

Create `web/src/lib/dictationInsert.ts`:

```ts
/**
 * Remove `removeLen` characters at `anchor` in `current` and splice in `insert`.
 * Used to replace a live interim transcript chunk with a longer interim/final one,
 * keeping dictated text anchored at the caret position where dictation began.
 */
export function spliceAt(current: string, anchor: number, removeLen: number, insert: string): string {
  return current.slice(0, anchor) + insert + current.slice(anchor + removeLen)
}
```

- [ ] **Step 4: Write the failing test for `autoGrow`**

Create `web/src/lib/__tests__/autoGrow.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { autoGrow } from '../autoGrow'

describe('autoGrow', () => {
  it('is a no-op and does not throw when element is null', () => {
    expect(() => autoGrow(null)).not.toThrow()
  })

  it('sets the textarea height based on scrollHeight, capped at maxPx', () => {
    const el = document.createElement('textarea')
    Object.defineProperty(el, 'scrollHeight', { value: 500, configurable: true })
    autoGrow(el, 192)
    expect(el.style.height).toBe('192px')
  })

  it('uses scrollHeight when below the cap', () => {
    const el = document.createElement('textarea')
    Object.defineProperty(el, 'scrollHeight', { value: 60, configurable: true })
    autoGrow(el, 192)
    expect(el.style.height).toBe('60px')
  })
})
```

- [ ] **Step 5: Run the test to verify it fails**

Run (from `web/`): `npx vitest run src/lib/__tests__/autoGrow.test.ts`
Expected: FAIL — `Failed to resolve import "../autoGrow"`.

- [ ] **Step 6: Implement `autoGrow`**

Create `web/src/lib/autoGrow.ts`:

```ts
/**
 * Auto-grow a textarea: reset to natural height, then size to content,
 * capped at `maxPx`. Mirrors the inline resize logic used by the prompt
 * surfaces so dictated text grows the box the same way typing does.
 */
export function autoGrow(el: HTMLTextAreaElement | null, maxPx = 192): void {
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, maxPx)}px`
}
```

- [ ] **Step 7: Run both tests to verify they pass**

Run (from `web/`): `npx vitest run src/lib/__tests__/dictationInsert.test.ts src/lib/__tests__/autoGrow.test.ts`
Expected: PASS (7 tests).

- [ ] **Step 8: Commit**

```bash
git add web/src/lib/dictationInsert.ts web/src/lib/autoGrow.ts web/src/lib/__tests__/dictationInsert.test.ts web/src/lib/__tests__/autoGrow.test.ts
git commit -m "feat(voice): pure helpers for cursor splice and textarea autogrow"
```

---

### Task 2: `useDictation` — low-level Web Speech wrapper

**Files:**
- Create: `web/src/lib/useDictation.ts`
- Test: `web/src/lib/__tests__/useDictation.test.tsx`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `interface UseDictationOptions { onResult: (text: string, isFinal: boolean) => void; lang?: string }`
  - `interface UseDictation { supported: boolean; listening: boolean; error: string | null; start(): void; stop(): void; toggle(): void }`
  - `useDictation(opts: UseDictationOptions): UseDictation` — wraps `window.SpeechRecognition ?? window.webkitSpeechRecognition`. `supported` is false when neither exists. On a recognition result, calls `onResult(transcript, true)` for each final result and `onResult(interimAccumulated, false)` once for interim text. Errors set `error` and stop listening. Never throws.

- [ ] **Step 1: Write the failing test**

Create `web/src/lib/__tests__/useDictation.test.tsx`:

```tsx
import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { useDictation } from '../useDictation'

// A controllable mock of the browser SpeechRecognition object.
class MockRecognition {
  lang = ''
  continuous = false
  interimResults = false
  onresult: ((e: unknown) => void) | null = null
  onerror: ((e: { error: string }) => void) | null = null
  onend: (() => void) | null = null
  start = vi.fn()
  stop = vi.fn(() => { this.onend?.() })

  // test helper: emit one result
  emit(transcript: string, isFinal: boolean) {
    this.onresult?.({
      resultIndex: 0,
      results: [Object.assign([{ transcript }], { isFinal })],
    })
  }
}

let instance: MockRecognition | null = null
function installMock() {
  instance = null
  vi.stubGlobal('SpeechRecognition', vi.fn(() => { instance = new MockRecognition(); return instance }))
}

afterEach(() => { vi.unstubAllGlobals(); instance = null })

describe('useDictation', () => {
  it('reports unsupported when no SpeechRecognition global exists', () => {
    vi.stubGlobal('SpeechRecognition', undefined)
    vi.stubGlobal('webkitSpeechRecognition', undefined)
    const { result } = renderHook(() => useDictation({ onResult: vi.fn() }))
    expect(result.current.supported).toBe(false)
  })

  it('starts listening and configures continuous + interim recognition', () => {
    installMock()
    const { result } = renderHook(() => useDictation({ onResult: vi.fn() }))
    expect(result.current.supported).toBe(true)
    act(() => { result.current.start() })
    expect(result.current.listening).toBe(true)
    expect(instance!.continuous).toBe(true)
    expect(instance!.interimResults).toBe(true)
    expect(instance!.lang).toBe('en-US')
    expect(instance!.start).toHaveBeenCalled()
  })

  it('forwards final and interim transcripts to onResult', () => {
    installMock()
    const onResult = vi.fn()
    const { result } = renderHook(() => useDictation({ onResult }))
    act(() => { result.current.start() })
    act(() => { instance!.emit('hello', false) })
    expect(onResult).toHaveBeenCalledWith('hello', false)
    act(() => { instance!.emit('hello world', true) })
    expect(onResult).toHaveBeenCalledWith('hello world', true)
  })

  it('stops listening on stop()', () => {
    installMock()
    const { result } = renderHook(() => useDictation({ onResult: vi.fn() }))
    act(() => { result.current.start() })
    act(() => { result.current.stop() })
    expect(result.current.listening).toBe(false)
  })

  it('records an error and stops listening on recognition error', () => {
    installMock()
    const { result } = renderHook(() => useDictation({ onResult: vi.fn() }))
    act(() => { result.current.start() })
    act(() => { instance!.onerror?.({ error: 'not-allowed' }) })
    expect(result.current.error).toBe('not-allowed')
    expect(result.current.listening).toBe(false)
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run (from `web/`): `npx vitest run src/lib/__tests__/useDictation.test.tsx`
Expected: FAIL — `Failed to resolve import "../useDictation"`.

- [ ] **Step 3: Implement `useDictation`**

Create `web/src/lib/useDictation.ts`:

```ts
import { useCallback, useEffect, useRef, useState } from 'react'

interface SpeechRecognitionResultEventLike {
  resultIndex: number
  results: ArrayLike<{ isFinal: boolean; [index: number]: { transcript: string } }>
}

interface SpeechRecognitionLike {
  lang: string
  continuous: boolean
  interimResults: boolean
  onresult: ((event: SpeechRecognitionResultEventLike) => void) | null
  onerror: ((event: { error: string }) => void) | null
  onend: (() => void) | null
  start(): void
  stop(): void
}

type SpeechRecognitionCtor = new () => SpeechRecognitionLike

function getCtor(): SpeechRecognitionCtor | null {
  if (typeof window === 'undefined') return null
  const w = window as unknown as {
    SpeechRecognition?: SpeechRecognitionCtor
    webkitSpeechRecognition?: SpeechRecognitionCtor
  }
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null
}

export interface UseDictationOptions {
  onResult: (text: string, isFinal: boolean) => void
  lang?: string
}

export interface UseDictation {
  supported: boolean
  listening: boolean
  error: string | null
  start(): void
  stop(): void
  toggle(): void
}

export function useDictation({ onResult, lang = 'en-US' }: UseDictationOptions): UseDictation {
  const ctorRef = useRef<SpeechRecognitionCtor | null>(getCtor())
  const recRef = useRef<SpeechRecognitionLike | null>(null)
  const [listening, setListening] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Keep the latest onResult without rebuilding the recognition instance.
  const onResultRef = useRef(onResult)
  useEffect(() => { onResultRef.current = onResult }, [onResult])

  const supported = ctorRef.current != null

  const stop = useCallback(() => {
    recRef.current?.stop()
  }, [])

  const start = useCallback(() => {
    const Ctor = ctorRef.current
    if (!Ctor || recRef.current) return
    setError(null)
    const rec = new Ctor()
    rec.lang = lang
    rec.continuous = true
    rec.interimResults = true
    rec.onresult = (event) => {
      let interim = ''
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const res = event.results[i]
        const transcript = res[0].transcript
        if (res.isFinal) onResultRef.current(transcript, true)
        else interim += transcript
      }
      if (interim) onResultRef.current(interim, false)
    }
    rec.onerror = (event) => { setError(event.error || 'speech-error'); setListening(false) }
    rec.onend = () => { recRef.current = null; setListening(false) }
    recRef.current = rec
    setListening(true)
    rec.start()
  }, [lang])

  const toggle = useCallback(() => {
    if (recRef.current) stop()
    else start()
  }, [start, stop])

  // Tear down any active recognition on unmount.
  useEffect(() => () => { recRef.current?.stop(); recRef.current = null }, [])

  return { supported, listening, error, start, stop, toggle }
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run (from `web/`): `npx vitest run src/lib/__tests__/useDictation.test.tsx`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add web/src/lib/useDictation.ts web/src/lib/__tests__/useDictation.test.tsx
git commit -m "feat(voice): useDictation hook wrapping the Web Speech API"
```

---

### Task 3: `useDictationField` — insert transcript into a controlled textarea

**Files:**
- Create: `web/src/lib/useDictationField.ts`
- Test: `web/src/lib/__tests__/useDictationField.test.tsx`

**Interfaces:**
- Consumes: `useDictation` / `UseDictation` (Task 2), `spliceAt` and `autoGrow` (Task 1).
- Produces:
  - `interface UseDictationFieldOptions { value: string; setValue: (v: string) => void; textareaRef: React.RefObject<HTMLTextAreaElement | null> }`
  - `useDictationField(opts: UseDictationFieldOptions): UseDictation` — captures the caret position when dictation starts, then inserts interim text live (replacing the prior interim chunk) and commits final text (with a trailing space) at that anchor, advancing the anchor. Returns the same shape as `useDictation` with `start`/`toggle` overridden to capture the caret.

- [ ] **Step 1: Write the failing test**

Create `web/src/lib/__tests__/useDictationField.test.tsx`:

```tsx
import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, render } from '@testing-library/react'
import { useRef, useState } from 'react'
import { useDictationField } from '../useDictationField'

class MockRecognition {
  lang = ''
  continuous = false
  interimResults = false
  onresult: ((e: unknown) => void) | null = null
  onerror: ((e: { error: string }) => void) | null = null
  onend: (() => void) | null = null
  start = vi.fn()
  stop = vi.fn(() => { this.onend?.() })
  emit(transcript: string, isFinal: boolean) {
    this.onresult?.({ resultIndex: 0, results: [Object.assign([{ transcript }], { isFinal })] })
  }
}

let instance: MockRecognition
afterEach(() => { vi.unstubAllGlobals() })

// A harness exposing the field hook over a real textarea + controlled state.
let api: ReturnType<typeof useDictationField>
let currentValue = ''
function Harness({ initial = '', caret }: { initial?: string; caret?: number }) {
  const [value, setValue] = useState(initial)
  currentValue = value
  const ref = useRef<HTMLTextAreaElement>(null)
  api = useDictationField({ value, setValue, textareaRef: ref })
  return (
    <textarea
      ref={ref}
      data-testid="ta"
      value={value}
      onChange={(e) => setValue(e.target.value)}
      // place the caret where the test wants it
      onFocus={(e) => { if (caret != null) e.currentTarget.setSelectionRange(caret, caret) }}
    />
  )
}

function installMock() {
  vi.stubGlobal('SpeechRecognition', vi.fn(() => { instance = new MockRecognition(); return instance }))
}

describe('useDictationField', () => {
  it('appends a final transcript at the end with a trailing space', () => {
    installMock()
    render(<Harness initial="" />)
    act(() => { api.start() })
    act(() => { instance.emit('hello world', true) })
    expect(currentValue).toBe('hello world ')
  })

  it('replaces interim text live, then commits the final text', () => {
    installMock()
    render(<Harness initial="" />)
    act(() => { api.start() })
    act(() => { instance.emit('hel', false) })
    expect(currentValue).toBe('hel')
    act(() => { instance.emit('hello', false) })
    expect(currentValue).toBe('hello')
    act(() => { instance.emit('hello there', true) })
    expect(currentValue).toBe('hello there ')
  })

  it('inserts at the caret position within existing text', () => {
    installMock()
    const { getByTestId } = render(<Harness initial="ab" caret={1} />)
    act(() => { (getByTestId('ta') as HTMLTextAreaElement).focus() })
    act(() => { api.start() })
    act(() => { instance.emit('X', true) })
    // caret was between 'a' and 'b' → "a" + "X " + "b"
    expect(currentValue).toBe('aX b')
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run (from `web/`): `npx vitest run src/lib/__tests__/useDictationField.test.tsx`
Expected: FAIL — `Failed to resolve import "../useDictationField"`.

- [ ] **Step 3: Implement `useDictationField`**

Create `web/src/lib/useDictationField.ts`:

```ts
import { useCallback, useRef } from 'react'
import { useDictation, type UseDictation } from './useDictation'
import { spliceAt } from './dictationInsert'
import { autoGrow } from './autoGrow'

export interface UseDictationFieldOptions {
  value: string
  setValue: (v: string) => void
  textareaRef: React.RefObject<HTMLTextAreaElement | null>
}

export function useDictationField({ value, setValue, textareaRef }: UseDictationFieldOptions): UseDictation {
  // Mirror the latest value so rapid result events between renders splice correctly.
  const valueRef = useRef(value)
  valueRef.current = value
  const anchorRef = useRef(0)   // caret index where dictation inserts
  const interimRef = useRef(0)  // length of the currently-shown interim chunk

  const onResult = useCallback((text: string, isFinal: boolean) => {
    const insert = isFinal ? `${text} ` : text
    const next = spliceAt(valueRef.current, anchorRef.current, interimRef.current, insert)
    valueRef.current = next
    if (isFinal) {
      anchorRef.current += insert.length
      interimRef.current = 0
    } else {
      interimRef.current = insert.length
    }
    setValue(next)
    if (typeof requestAnimationFrame === 'function') {
      requestAnimationFrame(() => autoGrow(textareaRef.current))
    }
  }, [setValue, textareaRef])

  const dictation = useDictation({ onResult })

  const start = useCallback(() => {
    const el = textareaRef.current
    anchorRef.current = el?.selectionStart ?? valueRef.current.length
    interimRef.current = 0
    dictation.start()
  }, [dictation, textareaRef])

  const toggle = useCallback(() => {
    if (dictation.listening) dictation.stop()
    else start()
  }, [dictation, start])

  return { ...dictation, start, toggle }
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run (from `web/`): `npx vitest run src/lib/__tests__/useDictationField.test.tsx`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add web/src/lib/useDictationField.ts web/src/lib/__tests__/useDictationField.test.tsx
git commit -m "feat(voice): useDictationField inserts transcript at the caret"
```

---

### Task 4: `MicButton` presentational component

**Files:**
- Create: `web/src/components/session/MicButton.tsx`
- Test: `web/src/components/session/__tests__/MicButton.test.tsx`

**Interfaces:**
- Consumes: `cn` from `@/lib/utils`, `Mic` from `lucide-react`.
- Produces: `MicButton(props: { supported: boolean; listening: boolean; error: string | null; onToggle: () => void })` — renders `null` when `!supported`; otherwise a button with `aria-label` `'Start dictation'`/`'Stop dictation'`, `aria-pressed={listening}`, and a red pulsing style while listening.

- [ ] **Step 1: Write the failing test**

Create `web/src/components/session/__tests__/MicButton.test.tsx`:

```tsx
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MicButton } from '../MicButton'

describe('MicButton', () => {
  it('renders nothing when speech is unsupported', () => {
    const { container } = render(
      <MicButton supported={false} listening={false} error={null} onToggle={() => {}} />,
    )
    expect(container.firstChild).toBeNull()
  })

  it('labels itself for starting when idle and fires onToggle', async () => {
    const onToggle = vi.fn()
    render(<MicButton supported listening={false} error={null} onToggle={onToggle} />)
    const btn = screen.getByRole('button', { name: 'Start dictation' })
    expect(btn).toHaveAttribute('aria-pressed', 'false')
    await userEvent.click(btn)
    expect(onToggle).toHaveBeenCalledTimes(1)
  })

  it('labels itself for stopping while listening', () => {
    render(<MicButton supported listening error={null} onToggle={() => {}} />)
    const btn = screen.getByRole('button', { name: 'Stop dictation' })
    expect(btn).toHaveAttribute('aria-pressed', 'true')
  })

  it('exposes the error via title when present', () => {
    render(<MicButton supported listening={false} error="not-allowed" onToggle={() => {}} />)
    expect(screen.getByRole('button')).toHaveAttribute('title', expect.stringContaining('not-allowed'))
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run (from `web/`): `npx vitest run src/components/session/__tests__/MicButton.test.tsx`
Expected: FAIL — `Failed to resolve import "../MicButton"`.

- [ ] **Step 3: Implement `MicButton`**

Create `web/src/components/session/MicButton.tsx`:

```tsx
import { Mic } from 'lucide-react'
import { cn } from '@/lib/utils'

interface Props {
  supported: boolean
  listening: boolean
  error: string | null
  onToggle: () => void
}

export function MicButton({ supported, listening, error, onToggle }: Props) {
  if (!supported) return null
  return (
    <button
      type="button"
      aria-label={listening ? 'Stop dictation' : 'Start dictation'}
      aria-pressed={listening}
      title={error ? `Voice error: ${error}` : listening ? 'Stop dictation' : 'Dictate'}
      onClick={onToggle}
      className={cn(
        'flex-shrink-0 rounded-md px-3 py-2 text-sm min-h-[44px] border',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
        listening
          ? 'border-red-400 bg-red-50 dark:bg-red-900/30 text-red-600 dark:text-red-400 animate-pulse'
          : 'border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800',
        error && !listening && 'border-amber-400 text-amber-600 dark:text-amber-400',
      )}
    >
      <Mic size={16} aria-hidden="true" />
    </button>
  )
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run (from `web/`): `npx vitest run src/components/session/__tests__/MicButton.test.tsx`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add web/src/components/session/MicButton.tsx web/src/components/session/__tests__/MicButton.test.tsx
git commit -m "feat(voice): MicButton presentational component"
```

---

### Task 5: Wire dictation into `Composer`

**Files:**
- Modify: `web/src/components/session/Composer.tsx`
- Test: `web/src/components/session/__tests__/Composer.voice.test.tsx`

**Interfaces:**
- Consumes: `useDictationField` (Task 3), `MicButton` (Task 4).
- Produces: no new exported symbols; `Composer` now renders a `MicButton` wired to `useDictationField({ value: text, setValue: setText, textareaRef })`.

- [ ] **Step 1: Write the failing integration test**

Create `web/src/components/session/__tests__/Composer.voice.test.tsx`:

```tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { Mock } from 'vitest'
import { Composer } from '../Composer'
import { useDictationField } from '@/lib/useDictationField'

vi.mock('@/lib/useDictationField', () => ({ useDictationField: vi.fn() }))
const mockField = useDictationField as unknown as Mock

const baseProps = {
  status: { state: 'idle', count: 0 },
  mode: 'default',
  onPrompt: vi.fn(),
  onInterrupt: vi.fn(),
  onSetMode: vi.fn(),
}

beforeEach(() => { mockField.mockReset() })

describe('Composer voice integration', () => {
  it('shows the mic button when dictation is supported and toggles it', async () => {
    const toggle = vi.fn()
    mockField.mockReturnValue({ supported: true, listening: false, error: null, start: vi.fn(), stop: vi.fn(), toggle })
    render(<Composer {...baseProps} />)
    const mic = screen.getByRole('button', { name: 'Start dictation' })
    await userEvent.click(mic)
    expect(toggle).toHaveBeenCalledTimes(1)
  })

  it('hides the mic button when dictation is unsupported', () => {
    mockField.mockReturnValue({ supported: false, listening: false, error: null, start: vi.fn(), stop: vi.fn(), toggle: vi.fn() })
    render(<Composer {...baseProps} />)
    expect(screen.queryByRole('button', { name: 'Start dictation' })).toBeNull()
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run (from `web/`): `npx vitest run src/components/session/__tests__/Composer.voice.test.tsx`
Expected: FAIL — `getByRole` cannot find a "Start dictation" button (MicButton not rendered yet).

- [ ] **Step 3: Add the imports**

In `web/src/components/session/Composer.tsx`, add to the import block near the top (after the existing `CommandPalette` import on line 6):

```tsx
import { MicButton } from './MicButton'
import { useDictationField } from '@/lib/useDictationField'
```

- [ ] **Step 4: Call the hook**

In `Composer.tsx`, immediately after `const att = useImageAttachments()` (line 30), add:

```tsx
  const voice = useDictationField({ value: text, setValue: setText, textareaRef })
```

- [ ] **Step 5: Render the MicButton**

In `Composer.tsx`, insert the `MicButton` right before the hidden file `<input ref={fileRef} …>` (currently line 168), so it sits just left of the attach button:

```tsx
        <MicButton
          supported={voice.supported}
          listening={voice.listening}
          error={voice.error}
          onToggle={voice.toggle}
        />

```

- [ ] **Step 6: Run the test to verify it passes**

Run (from `web/`): `npx vitest run src/components/session/__tests__/Composer.voice.test.tsx`
Expected: PASS (2 tests).

- [ ] **Step 7: Verify the existing Composer tests still pass**

Run (from `web/`): `npx vitest run src/components/session`
Expected: PASS (all session component tests, including pre-existing ones).

- [ ] **Step 8: Commit**

```bash
git add web/src/components/session/Composer.tsx web/src/components/session/__tests__/Composer.voice.test.tsx
git commit -m "feat(voice): mic dictation in the session composer"
```

---

### Task 6: Wire dictation into `DispatchBar`

**Files:**
- Modify: `web/src/components/DispatchBar.tsx`
- Test: `web/src/components/__tests__/DispatchBar.voice.test.tsx`

**Interfaces:**
- Consumes: `useDictationField` (Task 3), `MicButton` (Task 4).
- Produces: no new exported symbols; `DispatchBar` renders a `MicButton` wired to `useDictationField({ value: text, setValue: setText, textareaRef })`.

- [ ] **Step 1: Write the failing integration test**

Create `web/src/components/__tests__/DispatchBar.voice.test.tsx`:

```tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { Mock } from 'vitest'
import { DispatchBar, type DispatchBarProps } from '../DispatchBar'
import { useDictationField } from '@/lib/useDictationField'

vi.mock('@/lib/useDictationField', () => ({ useDictationField: vi.fn() }))
const mockField = useDictationField as unknown as Mock

const baseProps: DispatchBarProps = {
  repos: [],
  reposLoading: false,
  reposError: false,
  onDispatch: vi.fn(),
  onNavigate: vi.fn(),
  onOpenHighlighted: vi.fn(),
  onRowAction: vi.fn(),
  onShowHelp: vi.fn(),
  onEscape: vi.fn(),
  isPending: false,
}

beforeEach(() => { mockField.mockReset() })

describe('DispatchBar voice integration', () => {
  it('shows the mic button when supported and toggles it', async () => {
    const toggle = vi.fn()
    mockField.mockReturnValue({ supported: true, listening: false, error: null, start: vi.fn(), stop: vi.fn(), toggle })
    render(<DispatchBar {...baseProps} />)
    await userEvent.click(screen.getByRole('button', { name: 'Start dictation' }))
    expect(toggle).toHaveBeenCalledTimes(1)
  })

  it('hides the mic button when unsupported', () => {
    mockField.mockReturnValue({ supported: false, listening: false, error: null, start: vi.fn(), stop: vi.fn(), toggle: vi.fn() })
    render(<DispatchBar {...baseProps} />)
    expect(screen.queryByRole('button', { name: 'Start dictation' })).toBeNull()
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run (from `web/`): `npx vitest run src/components/__tests__/DispatchBar.voice.test.tsx`
Expected: FAIL — no "Start dictation" button found.

- [ ] **Step 3: Add the imports**

In `web/src/components/DispatchBar.tsx`, add after the existing `AttachmentChips` import (line 7):

```tsx
import { MicButton } from './session/MicButton'
import { useDictationField } from '@/lib/useDictationField'
```

- [ ] **Step 4: Call the hook**

In `DispatchBar.tsx`, immediately after `const att = useImageAttachments()` (line 55), add:

```tsx
  const voice = useDictationField({ value: text, setValue: setText, textareaRef })
```

- [ ] **Step 5: Render the MicButton**

In `DispatchBar.tsx`, insert the `MicButton` right before the hidden file `<input ref={fileRef} …>` (currently line 284), so it sits just left of the attach button:

```tsx
        <MicButton
          supported={voice.supported}
          listening={voice.listening}
          error={voice.error}
          onToggle={voice.toggle}
        />

```

- [ ] **Step 6: Run the test to verify it passes**

Run (from `web/`): `npx vitest run src/components/__tests__/DispatchBar.voice.test.tsx`
Expected: PASS (2 tests).

- [ ] **Step 7: Verify existing DispatchBar tests still pass**

Run (from `web/`): `npx vitest run src/components/__tests__/DispatchBar.test.tsx`
Expected: PASS (all pre-existing DispatchBar tests).

- [ ] **Step 8: Commit**

```bash
git add web/src/components/DispatchBar.tsx web/src/components/__tests__/DispatchBar.voice.test.tsx
git commit -m "feat(voice): mic dictation in the dispatch bar"
```

---

### Task 7: Final verification — full suite, types, lint

**Files:** none (verification only).

- [ ] **Step 1: Run the full web test suite**

Run (from `web/`): `npx vitest run`
Expected: PASS — all tests, including the new voice tests and all pre-existing tests.

- [ ] **Step 2: Type-check**

Run (from `web/`): `npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 3: Lint the new and modified files**

Run (from `web/`):
```bash
npx eslint src/lib/dictationInsert.ts src/lib/autoGrow.ts src/lib/useDictation.ts src/lib/useDictationField.ts \
  src/components/session/MicButton.tsx src/components/session/Composer.tsx src/components/DispatchBar.tsx
```
Expected: no errors.

- [ ] **Step 4: Fix any failures**

If `tsc`/`eslint`/tests report issues, fix them in the relevant file and re-run the failing command until green. (No code shown here — the fix depends on the specific failure.)

- [ ] **Step 5: Final commit (only if Step 4 changed files)**

```bash
git add -A
git commit -m "chore(voice): satisfy type-check and lint"
```

---

## Self-Review

**Spec coverage:**
- Dictation-only, browser Web Speech API → Task 2 (`useDictation`). ✓
- Both surfaces (DispatchBar + Composer) via one shared hook → Tasks 5 & 6 use `useDictationField` (Task 3). ✓
- Append at cursor, interim replaced by final → Task 3 (`spliceAt` + anchor/interim refs), tested. ✓
- No auto-send → integration only fills `text`; submit paths untouched. ✓
- `lang: 'en-US'` hardcoded → Task 2 default. ✓
- Graceful hiding when unsupported → `MicButton` returns null (Task 4) + `supported` from `useDictation`. ✓
- Error handling never throws → Task 2 `onerror` sets state; tested. ✓
- Testing for hook/button/integration → Tasks 1–6 each include tests. ✓
- No backend / no new deps → all files under `web/src`, only existing `lucide-react`/`@/lib/utils`. ✓

**Placeholder scan:** No TBD/TODO; Task 7 Step 4 is the only "depends on failure" step and is explicitly verification-cleanup, not a deliverable. ✓

**Type consistency:** `UseDictation` shape is identical across `useDictation` (Task 2), `useDictationField` return (Task 3), and the `MicButton` props subset (`supported`/`listening`/`error`/`onToggle`). `spliceAt(current, anchor, removeLen, insert)` signature matches its call in `useDictationField`. `useDictationField({ value, setValue, textareaRef })` matches calls in Tasks 5 & 6. ✓
