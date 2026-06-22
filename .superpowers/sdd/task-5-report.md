# Task 5 Report: Composer ↑/↓ history recall with draft preservation

## TDD Evidence

### RED — 5 new tests failing

```
node_modules/.bin/vitest run src/components/session/__tests__/Composer.test.tsx -t "recall|history|draft"

 FAIL  src/components/session/__tests__/Composer.test.tsx > Composer > ArrowDown walks back toward the live draft and restores it
 FAIL  src/components/session/__tests__/Composer.test.tsx > Composer > typing after a recall detaches history (next ArrowUp starts from newest)
 FAIL  src/components/session/__tests__/Composer.test.tsx > Composer > ArrowUp on empty input recalls the most recent prompt, then older

Tests  3 failed | 2 passed | 26 skipped (31)
```

(The "no history" and "palette" tests were already passing because ArrowUp on empty + no history === no-op, and the palette test showed `/g` unchanged for unrelated reasons.)

### GREEN — all 31 tests passing

```
node_modules/.bin/vitest run src/components/session/__tests__/Composer.test.tsx

 Test Files  1 passed (1)
      Tests  31 passed (31)
```

---

## Edits to Composer.tsx

### 1. Added import
```tsx
import { caretLineEdges } from '@/lib/caretLineEdges'
```

### 2. Added `history?: string[]` to `interface Props` and destructured with default `= []`

### 3. Added recall state near other hooks

Used `useRef` (not `useState`) for `histPosRef` to avoid the `react-hooks/set-state-in-effect` lint error that fires when calling `setHistPos(0)` inside the `autoFocusKey` effect. Since `histPos` is only read in event handlers and never drives JSX rendering directly, a ref is semantically correct and ESLint-compliant.

```tsx
const histPosRef = useRef(0)
const draftRef = useRef('')
const caretToEndRef = useRef(false)
```

### 4. Combined autoFocusKey effect — adds recall reset alongside focus

```tsx
useEffect(() => {
  textareaRef.current?.focus()
  histPosRef.current = 0
  draftRef.current = ''
}, [autoFocusKey])
```

### 5. Added caret-to-end effect after programmatic recall

```tsx
useEffect(() => {
  if (!caretToEndRef.current) return
  caretToEndRef.current = false
  const el = textareaRef.current
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, 192)}px`
  const end = el.value.length
  el.setSelectionRange(end, end)
}, [text])
```

### 6. Added `recallTo` callback

```tsx
const recallTo = useCallback((pos: number, value: string) => {
  caretToEndRef.current = true
  histPosRef.current = pos
  setText(value)
}, [])
```

### 7. `handleChange` — detach on real user edit
Added `if (histPosRef.current !== 0) histPosRef.current = 0` as first line.

### 8. `submit` — reset recall after clear
Added `histPosRef.current = 0; draftRef.current = ''` after each `setText('')` call.

### 9. `handleKeyDown` — recall branch after palette check, before Enter check

```tsx
if ((e.key === 'ArrowUp' || e.key === 'ArrowDown') && history.length > 0) {
  const el = e.currentTarget
  const caret = el.selectionStart ?? 0
  const empty = text === ''
  const { atFirstLine, atLastLine } = caretLineEdges(text, caret)
  if (e.key === 'ArrowUp' && (empty || atFirstLine) && histPosRef.current < history.length) {
    if (histPosRef.current === 0) draftRef.current = text
    e.preventDefault()
    recallTo(histPosRef.current + 1, history[history.length - (histPosRef.current + 1)])
    return
  }
  if (e.key === 'ArrowDown' && (empty || atLastLine) && histPosRef.current > 0) {
    const pos = histPosRef.current - 1
    e.preventDefault()
    recallTo(pos, pos === 0 ? draftRef.current : history[history.length - pos])
    return
  }
}
```

---

## Files Changed

- `web/src/components/session/Composer.tsx` — implementation (+73 lines net)
- `web/src/components/session/__tests__/Composer.test.tsx` — 5 new tests (+55 lines)

---

## Full Suite + Quality Gates

| Check | Result |
|---|---|
| `vitest run __tests__/Composer.test.tsx` | 31/31 passed |
| `vitest run` (full suite) | 770/770 passed (102 files) |
| `tsc --noEmit` | clean (no output) |
| `eslint Composer.tsx __tests__/Composer.test.tsx` | clean (no output) |

---

## Commit

```
0a77681 feat(composer): ↑/↓ command-history recall with draft preservation
```

---

## Notes

**ESLint deviation from brief:** The brief showed `histPos` as `useState`. The `react-hooks/set-state-in-effect` rule in this project's ESLint config prohibits calling `setState` synchronously inside an effect body. Since `histPos` is never used in JSX (only in event handlers), converting to `useRef` is the correct React idiom and satisfies the lint rule without weakening any assertion.

---

## Bug Fix: Unmatched slash-command guard (post-Task-5 review)

**Bug:** In `handleKeyDown`, when `pickingCommand` is true but the palette returns `false` (zero matches for the query, e.g. `/zzz`), execution fell through to the history-recall branch and silently replaced the typed slash text with a history entry.

**RED (before fix — test added, guard not yet applied):**
```
FAIL  src/components/session/__tests__/Composer.test.tsx > Composer > does not recall history when slash-prefix matches NO command (unmatched slash)

Expected the element to have value:
  /zzz
Received:
  newer

Tests  1 failed | 31 passed (32)
```

**Fix:** Added `!pickingCommand` guard to the recall branch:
```tsx
// Before:
if ((e.key === 'ArrowUp' || e.key === 'ArrowDown') && history.length > 0) {
// After:
if (!pickingCommand && (e.key === 'ArrowUp' || e.key === 'ArrowDown') && history.length > 0) {
```

**GREEN (after fix):**
```
Tests  32 passed (32)
```

**Full suite + quality gates after fix:**

| Check | Result |
|---|---|
| `vitest run __tests__/Composer.test.tsx` | 32/32 passed |
| `vitest run` (full suite) | 771/771 passed (102 files) |
| `tsc --noEmit` | clean (no output) |
| `eslint .` | clean (no output) |
