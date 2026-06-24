# Mobile Camera Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let mobile users capture a photo with their camera (or pick from library) to attach to a prompt, with iPhone HEIC photos converted to JPEG client-side so they upload successfully.

**Architecture:** Broaden the two hidden file inputs to `accept="image/*"` (no `capture` attribute) so iOS Safari and Android Chrome show the native "Take Photo / Choose from Library" sheet. Add a lazy-loaded client-side HEIC→JPEG conversion step in front of the existing upload pipeline; the backend and Claude SDK path are unchanged (they already accept only png/jpeg/webp/gif).

**Tech Stack:** React + TypeScript + Vite, Vitest + @testing-library/react, `heic2any` (libheif wasm, dynamic-imported).

## Global Constraints

- All commands run from `web/` unless stated otherwise.
- Real type gate is `npx tsc -b --force` (root `tsc --noEmit` checks nothing). Build is `npm run build`.
- Lint gate: `npm run lint` (`eslint . --ext ts,tsx --max-warnings 0`); CI fails on any warning.
- Full test gate: `npx vitest run` (CI runs the full suite, not a subset).
- `node_modules` in this worktree is a **symlink** to the deploy checkout's copy; use `./node_modules/.bin/*` (not `npx` for installed bins). `heic2any` is dynamic-imported and **mocked in tests**, so vitest does not need it physically installed; an ambient `declare module 'heic2any'` lets `tsc -b` pass without it installed; only `npm run build`/CI needs the real package, which `npm ci` installs from the committed lockfile.
- Existing invariants in `useImageAttachments.ts` are unchanged: `ALLOWED = ['image/png','image/jpeg','image/gif','image/webp']`, `MAX_BYTES = 10*1024*1024`, `MAX_IMAGES = 5`.
- Do NOT add the `capture` attribute (it would force camera-only and remove the library option).
- No server changes.

---

### Task 1: HEIC→JPEG conversion helper

**Files:**
- Create: `web/src/components/session/heicConvert.ts`
- Create: `web/src/heic2any.d.ts`
- Create: `web/src/components/session/__tests__/heicConvert.test.tsx`
- Modify: `web/package.json` (add `heic2any` dependency) and `web/package-lock.json`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `isHeic(file: File): boolean`
  - `maybeConvertHeic(file: File): Promise<File>` — returns the input unchanged for non-HEIC; for HEIC returns a new `File` named `*.jpg` with `type: 'image/jpeg'`; rejects if conversion fails.

- [ ] **Step 1: Add the dependency**

Run (from `web/`):
```bash
./node_modules/.bin/npm --version >/dev/null 2>&1; npm install heic2any@0.0.4
```
Expected: `package.json` gains `"heic2any": "0.0.4"` under dependencies and `package-lock.json` updates.
If the registry is unreachable in this environment, manually add `"heic2any": "0.0.4"` to `web/package.json` dependencies and add the corresponding `package-lock.json` entry (the ambient declaration in Step 2 keeps `tsc`/tests green without it installed; CI's `npm ci` installs it for the build).

- [ ] **Step 2: Add an ambient module declaration**

Create `web/src/heic2any.d.ts`:
```ts
// heic2any ships its own types once installed; this ambient declaration lets
// `tsc -b` resolve the dynamic import even when the package is absent from the
// worktree's symlinked node_modules. It is harmless when the real types exist.
declare module 'heic2any'
```

- [ ] **Step 3: Write the failing test**

Create `web/src/components/session/__tests__/heicConvert.test.tsx`:
```tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'

const heic2any = vi.fn(async () => new Blob([new Uint8Array(4)], { type: 'image/jpeg' }))
vi.mock('heic2any', () => ({ default: heic2any }))

import { isHeic, maybeConvertHeic } from '../heicConvert'

beforeEach(() => { heic2any.mockClear() })

function file(name: string, type: string) {
  return new File([new Uint8Array(8)], name, { type })
}

describe('isHeic', () => {
  it('detects by mime type', () => {
    expect(isHeic(file('x.bin', 'image/heic'))).toBe(true)
    expect(isHeic(file('x.bin', 'image/heif'))).toBe(true)
  })
  it('detects by extension when type is empty', () => {
    expect(isHeic(file('IMG_1.HEIC', ''))).toBe(true)
    expect(isHeic(file('clip.heif', ''))).toBe(true)
  })
  it('returns false for normal images', () => {
    expect(isHeic(file('x.png', 'image/png'))).toBe(false)
  })
})

describe('maybeConvertHeic', () => {
  it('converts a HEIC file to a jpeg File', async () => {
    const out = await maybeConvertHeic(file('photo.heic', 'image/heic'))
    expect(heic2any).toHaveBeenCalledOnce()
    expect(out.type).toBe('image/jpeg')
    expect(out.name).toBe('photo.jpg')
  })
  it('converts when detected by extension with empty type', async () => {
    const out = await maybeConvertHeic(file('IMG_1.HEIC', ''))
    expect(out.name).toBe('IMG_1.jpg')
    expect(out.type).toBe('image/jpeg')
  })
  it('passes a non-HEIC file through unchanged without calling heic2any', async () => {
    const png = file('x.png', 'image/png')
    const out = await maybeConvertHeic(png)
    expect(out).toBe(png)
    expect(heic2any).not.toHaveBeenCalled()
  })
  it('handles heic2any returning a Blob array (multi-image HEIC)', async () => {
    heic2any.mockResolvedValueOnce([new Blob([new Uint8Array(4)], { type: 'image/jpeg' })] as unknown as Blob)
    const out = await maybeConvertHeic(file('live.heic', 'image/heic'))
    expect(out.type).toBe('image/jpeg')
  })
  it('rejects when conversion throws', async () => {
    heic2any.mockRejectedValueOnce(new Error('decode failed'))
    await expect(maybeConvertHeic(file('bad.heic', 'image/heic'))).rejects.toThrow()
  })
})
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `./node_modules/.bin/vitest run src/components/session/__tests__/heicConvert.test.tsx`
Expected: FAIL — cannot resolve `../heicConvert` / `isHeic is not a function`.

- [ ] **Step 5: Implement the helper**

Create `web/src/components/session/heicConvert.ts`:
```ts
// web/src/components/session/heicConvert.ts
// Client-side HEIC/HEIF → JPEG conversion. iPhones capture HEIC by default,
// which neither the attachment server (png/jpeg/webp/gif only) nor Claude can
// consume, so we convert in the browser before upload. heic2any (libheif wasm)
// is dynamic-imported so its payload stays out of the main bundle.

const HEIC_TYPES = ['image/heic', 'image/heif']
const HEIC_EXT = /\.(heic|heif)$/i

export function isHeic(file: File): boolean {
  return HEIC_TYPES.includes(file.type.toLowerCase()) || HEIC_EXT.test(file.name)
}

export async function maybeConvertHeic(file: File): Promise<File> {
  if (!isHeic(file)) return file
  const { default: heic2any } = await import('heic2any')
  const out = await heic2any({ blob: file, toType: 'image/jpeg', quality: 0.9 })
  const blob = Array.isArray(out) ? out[0] : out
  const base = file.name.replace(HEIC_EXT, '')
  return new File([blob], `${base}.jpg`, { type: 'image/jpeg' })
}
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `./node_modules/.bin/vitest run src/components/session/__tests__/heicConvert.test.tsx`
Expected: PASS (all cases).

- [ ] **Step 7: Type-check and lint**

Run: `npx tsc -b --force && npm run lint`
Expected: no errors, no warnings.

- [ ] **Step 8: Commit**

```bash
git add web/src/components/session/heicConvert.ts web/src/heic2any.d.ts \
  web/src/components/session/__tests__/heicConvert.test.tsx \
  web/package.json web/package-lock.json
git commit -m "feat(web): client-side HEIC→JPEG conversion helper

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Convert HEIC inside the attachment upload pipeline

**Files:**
- Modify: `web/src/components/session/useImageAttachments.ts:22-60` (`addFiles`)
- Modify: `web/src/components/session/__tests__/useImageAttachments.test.tsx`

**Interfaces:**
- Consumes: `maybeConvertHeic(file: File): Promise<File>` from Task 1.
- Produces: no API change to the hook; `addFiles` now converts HEIC before validation/upload and emits an error chip (`error: 'Couldn’t read photo'`) when conversion fails.

- [ ] **Step 1: Write the failing tests**

Add to `web/src/components/session/__tests__/useImageAttachments.test.tsx`. At the top of the file, after the existing imports, add the heic2any mock:
```tsx
const heic2any = vi.fn(async () => new Blob([new Uint8Array(10)], { type: 'image/jpeg' }))
vi.mock('heic2any', () => ({ default: heic2any }))
```
Then add inside the `describe('useImageAttachments', ...)` block:
```tsx
it('converts a HEIC capture to JPEG and uploads it', async () => {
  const { result } = renderHook(() => useImageAttachments())
  const heic = new File([new Uint8Array(10)], 'photo.heic', { type: 'image/heic' })
  await act(async () => { await result.current.addFiles([heic]) })
  await waitFor(() => expect(result.current.attachments).toHaveLength(1))
  expect(heic2any).toHaveBeenCalledOnce()
  expect(result.current.attachments[0].error).toBeUndefined()
  expect(result.current.refs()).toEqual(['att-1'])
})

it('shows an error chip when HEIC conversion fails', async () => {
  heic2any.mockRejectedValueOnce(new Error('decode failed'))
  const fetchSpy = vi.fn(async () =>
    ({ ok: true, json: async () => ({ id: 'att-1', mediaType: 'image/png', filename: 'x.png', byteSize: 10, url: '/x' }) }))
  vi.stubGlobal('fetch', fetchSpy)
  const { result } = renderHook(() => useImageAttachments())
  const heic = new File([new Uint8Array(10)], 'photo.heic', { type: 'image/heic' })
  await act(async () => { await result.current.addFiles([heic]) })
  await waitFor(() => expect(result.current.attachments).toHaveLength(1))
  expect(result.current.attachments[0].error).toMatch(/read photo/i)
  expect(fetchSpy).not.toHaveBeenCalled()
  expect(result.current.refs()).toHaveLength(0)
})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./node_modules/.bin/vitest run src/components/session/__tests__/useImageAttachments.test.tsx`
Expected: FAIL — the HEIC file is treated as unsupported (`error: 'Unsupported file type'`) because `maybeConvertHeic` isn't wired in yet.

- [ ] **Step 3: Wire conversion into `addFiles`**

In `web/src/components/session/useImageAttachments.ts`, add the import at the top (after the React import):
```ts
import { maybeConvertHeic } from './heicConvert'
```
Replace the body of the `for (const file of list)` loop. The current loop is:
```ts
    for (const file of list) {
      if (accepted >= MAX_IMAGES) break
      const previewUrl = URL.createObjectURL(file)
      if (!ALLOWED.includes(file.type)) {
```
Change it to convert first, renaming the loop variable to `original`:
```ts
    for (const original of list) {
      if (accepted >= MAX_IMAGES) break
      let file: File
      try {
        file = await maybeConvertHeic(original)
      } catch {
        setAttachments((a) => [...a, {
          key: _nextKey(), mime: original.type, name: original.name,
          previewUrl: URL.createObjectURL(original), error: 'Couldn’t read photo' }])
        accepted++
        continue
      }
      const previewUrl = URL.createObjectURL(file)
      if (!ALLOWED.includes(file.type)) {
```
Leave the remainder of the loop (the `ALLOWED` check body, the size check, and the upload `try/catch`) unchanged — it already references `file`, `previewUrl`, `file.size`, `file.type`, `file.name`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./node_modules/.bin/vitest run src/components/session/__tests__/useImageAttachments.test.tsx`
Expected: PASS (new cases + all pre-existing cases).

- [ ] **Step 5: Full suite, type-check, lint**

Run: `npx vitest run && npx tsc -b --force && npm run lint`
Expected: all green, no warnings.

- [ ] **Step 6: Commit**

```bash
git add web/src/components/session/useImageAttachments.ts \
  web/src/components/session/__tests__/useImageAttachments.test.tsx
git commit -m "feat(web): convert HEIC captures to JPEG before upload

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Broaden file inputs to `accept="image/*"` for the native camera sheet

**Files:**
- Modify: `web/src/components/session/Composer.tsx` (the `accept="image/png,image/jpeg,image/gif,image/webp"` line on the hidden `<input type="file">`)
- Modify: `web/src/components/DispatchBar.tsx` (same attribute on its hidden file input)
- Modify: `web/src/components/session/__tests__/Composer.images.test.tsx`
- Modify: `web/src/components/__tests__/DispatchBar.images.test.tsx`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: both composers expose `accept="image/*"` and keep `multiple`; no `capture` attribute.

- [ ] **Step 1: Write the failing assertions**

In `web/src/components/session/__tests__/Composer.images.test.tsx`, add a test inside the `describe` block (the `render` here mirrors the existing tests; capture its return value):
```tsx
it('file input uses image/* so mobile shows the camera option, and is not camera-only', () => {
  const { container } = render(<Composer status={{ state: 'idle', count: 0 }}
    onPrompt={() => {}} onInterrupt={() => {}} />)
  const input = container.querySelector('input[type=file]') as HTMLInputElement
  expect(input.getAttribute('accept')).toBe('image/*')
  expect(input.hasAttribute('capture')).toBe(false)
  expect(input.multiple).toBe(true)
})
```
In `web/src/components/__tests__/DispatchBar.images.test.tsx`, add an equivalent test. First open the file and copy its existing `render(<DispatchBar .../>)` call verbatim (use the same props the other tests in that file use), then assert:
```tsx
it('file input uses image/* and is not camera-only', () => {
  const { container } = render(/* same <DispatchBar .../> as the other tests in this file */)
  const input = container.querySelector('input[type=file]') as HTMLInputElement
  expect(input.getAttribute('accept')).toBe('image/*')
  expect(input.hasAttribute('capture')).toBe(false)
  expect(input.multiple).toBe(true)
})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
./node_modules/.bin/vitest run \
  src/components/session/__tests__/Composer.images.test.tsx \
  src/components/__tests__/DispatchBar.images.test.tsx
```
Expected: FAIL — `accept` is currently `"image/png,image/jpeg,image/gif,image/webp"`, not `"image/*"`.

- [ ] **Step 3: Update both inputs**

In `web/src/components/session/Composer.tsx`, change:
```tsx
          accept="image/png,image/jpeg,image/gif,image/webp"
```
to:
```tsx
          accept="image/*"
```
Do the same single-line change in `web/src/components/DispatchBar.tsx`. Do not add a `capture` attribute; leave `multiple` in place.

- [ ] **Step 4: Run the tests to verify they pass**

Run:
```bash
./node_modules/.bin/vitest run \
  src/components/session/__tests__/Composer.images.test.tsx \
  src/components/__tests__/DispatchBar.images.test.tsx
```
Expected: PASS.

- [ ] **Step 5: Full suite, type-check, lint**

Run: `npx vitest run && npx tsc -b --force && npm run lint`
Expected: all green, no warnings.

- [ ] **Step 6: Commit**

```bash
git add web/src/components/session/Composer.tsx web/src/components/DispatchBar.tsx \
  web/src/components/session/__tests__/Composer.images.test.tsx \
  web/src/components/__tests__/DispatchBar.images.test.tsx
git commit -m "feat(web): broaden attach inputs to image/* for native mobile camera sheet

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Native camera sheet via `accept="image/*"`, no `capture` → Task 3. ✓
- Client-side HEIC→JPEG conversion (lazy heic2any) → Task 1 + wired in Task 2. ✓
- Detection by type AND extension; size re-checked post-conversion (existing check runs on converted `file`); error chip on failure → Task 1 + Task 2. ✓
- `heic2any` dependency, lazy-loaded → Task 1. ✓
- Server unchanged → no task, by design. ✓
- Tests for helper + hook → Tasks 1 & 2; input-attribute tests → Task 3. ✓
- `ALLOWED`/`MAX_BYTES`/`MAX_IMAGES` unchanged → preserved in Task 2. ✓

**Placeholder scan:** Task 3 Step 1 DispatchBar render is intentionally "copy the existing render call" because the props live in that test file; every code-bearing step otherwise shows full code. No TBD/TODO.

**Type consistency:** `isHeic`/`maybeConvertHeic` signatures match between Task 1 definition and Task 2 usage. The hook's public API (`addFiles`, `refs`, `attachments[].error`) is unchanged; tests reference the same `error` field used elsewhere.
