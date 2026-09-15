import { act, renderHook } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { RepoCheckResponse } from '@/api/types'
import { useRepoSuggestion } from './useRepoSuggestion'

// ≥ 30 chars (the default minChars).
const LONG = 'fix the KinDash task modal footer buttons'
const OTHER = 'add a Postgres cutover runbook for dobby'
const KINDASH: RepoCheckResponse = { suggested_repo: 'cairnea/KinDash', reason: 'mentions KinDash' }

type Check = (prompt: string, repo: string | null) => Promise<RepoCheckResponse | null>

function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

function setup(check: Check, init: { text?: string; repo?: string; enabled?: boolean } = {}) {
  return renderHook(
    (p: { text: string; repo: string; enabled: boolean }) =>
      useRepoSuggestion({ ...p, check }),
    { initialProps: { text: init.text ?? '', repo: init.repo ?? 'steiner385/viberpit', enabled: init.enabled ?? true } },
  )
}

/** Let the debounce fire and the resulting promise settle. */
async function idle(ms = 1500) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms) })
}

describe('useRepoSuggestion', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => {
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it('debounces: fires once after the text goes idle, with the trimmed text and repo', async () => {
    const check = vi.fn<Check>().mockResolvedValue(KINDASH)
    const h = setup(check, { text: LONG })
    h.rerender({ text: LONG + ' a', repo: 'steiner385/viberpit', enabled: true })
    await idle(500)
    h.rerender({ text: LONG + ' ab ', repo: 'steiner385/viberpit', enabled: true })
    await idle(500)
    expect(check).not.toHaveBeenCalled()
    await idle(1500)
    expect(check).toHaveBeenCalledTimes(1)
    expect(check).toHaveBeenCalledWith(LONG + ' ab', 'steiner385/viberpit')
    expect(h.result.current.suggestion).toEqual({ repo: 'cairnea/KinDash', reason: 'mentions KinDash' })
  })

  it('never fires below minChars', async () => {
    const check = vi.fn<Check>().mockResolvedValue(KINDASH)
    const h = setup(check, { text: 'fix KinDash' })
    await idle(5000)
    expect(check).not.toHaveBeenCalled()
    expect(h.result.current.suggestion).toBeNull()
  })

  it('passes null for an empty repo', async () => {
    const check = vi.fn<Check>().mockResolvedValue(null)
    setup(check, { text: LONG, repo: '' })
    await idle()
    expect(check).toHaveBeenCalledWith(LONG, null)
  })

  it('caches by trimmed text so the same text never refires', async () => {
    const check = vi.fn<Check>().mockResolvedValue(KINDASH)
    const h = setup(check, { text: LONG })
    await idle()
    h.rerender({ text: OTHER, repo: 'steiner385/viberpit', enabled: true })
    await idle()
    h.rerender({ text: `  ${LONG}  `, repo: 'steiner385/viberpit', enabled: true })
    await idle()
    expect(check).toHaveBeenCalledTimes(2)
    expect(check.mock.calls.map((c) => c[0])).toEqual([LONG, OTHER])
    expect(h.result.current.suggestion).toEqual({ repo: 'cairnea/KinDash', reason: 'mentions KinDash' })
  })

  it('hides a suggestion equal to the current repo', async () => {
    const check = vi.fn<Check>().mockResolvedValue(KINDASH)
    const h = setup(check, { text: LONG, repo: 'cairnea/KinDash' })
    await idle()
    expect(check).toHaveBeenCalledTimes(1)
    expect(h.result.current.suggestion).toBeNull()
    // Switching away from it makes the cached answer relevant again.
    h.rerender({ text: LONG, repo: 'steiner385/viberpit', enabled: true })
    expect(h.result.current.suggestion?.repo).toBe('cairnea/KinDash')
  })

  it('dismiss hides the suggestion until the text changes', async () => {
    const check = vi.fn<Check>().mockResolvedValue(KINDASH)
    const h = setup(check, { text: LONG })
    await idle()
    expect(h.result.current.suggestion).not.toBeNull()
    act(() => h.result.current.dismiss())
    expect(h.result.current.suggestion).toBeNull()
    expect(await h.result.current.ensureChecked()).toBeNull()
    h.rerender({ text: OTHER, repo: 'steiner385/viberpit', enabled: true })
    await idle()
    expect(h.result.current.suggestion).not.toBeNull()
  })

  it('ignores a stale response for text that is no longer current', async () => {
    const first = deferred<RepoCheckResponse | null>()
    const second = deferred<RepoCheckResponse | null>()
    const check = vi.fn<Check>()
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise)
    const h = setup(check, { text: LONG })
    await idle()
    h.rerender({ text: OTHER, repo: 'steiner385/viberpit', enabled: true })
    await idle()
    expect(check).toHaveBeenCalledTimes(2)
    await act(async () => { first.resolve(KINDASH) })
    expect(h.result.current.suggestion).toBeNull()
    await act(async () => { second.resolve({ suggested_repo: 'cairnea/infra', reason: null }) })
    expect(h.result.current.suggestion).toEqual({ repo: 'cairnea/infra', reason: null })
  })

  describe('ensureChecked', () => {
    it('returns the cached result without refiring', async () => {
      const check = vi.fn<Check>().mockResolvedValue(KINDASH)
      const h = setup(check, { text: LONG })
      await idle()
      expect(check).toHaveBeenCalledTimes(1)
      let out: unknown
      await act(async () => { out = await h.result.current.ensureChecked() })
      expect(out).toEqual({ repo: 'cairnea/KinDash', reason: 'mentions KinDash' })
      expect(check).toHaveBeenCalledTimes(1)
    })

    it('awaits an in-flight check for the current text', async () => {
      const d = deferred<RepoCheckResponse | null>()
      const check = vi.fn<Check>().mockReturnValue(d.promise)
      const h = setup(check, { text: LONG })
      await idle()
      expect(check).toHaveBeenCalledTimes(1)
      let out: unknown = 'unset'
      const p = h.result.current.ensureChecked().then((r) => { out = r })
      expect(check).toHaveBeenCalledTimes(1)
      await act(async () => { d.resolve(KINDASH); await p })
      expect(out).toEqual({ repo: 'cairnea/KinDash', reason: 'mentions KinDash' })
      expect(check).toHaveBeenCalledTimes(1)
    })

    it('fires immediately when nothing is cached or in flight', async () => {
      const check = vi.fn<Check>().mockResolvedValue(KINDASH)
      const h = setup(check, { text: LONG })
      expect(check).not.toHaveBeenCalled()
      let out: unknown
      await act(async () => { out = await h.result.current.ensureChecked() })
      expect(check).toHaveBeenCalledTimes(1)
      expect(out).toEqual({ repo: 'cairnea/KinDash', reason: 'mentions KinDash' })
      // The pending debounce must not fire a second request for the same text.
      await idle()
      expect(check).toHaveBeenCalledTimes(1)
    })

    it('returns null when the suggestion equals the current repo', async () => {
      const check = vi.fn<Check>().mockResolvedValue(KINDASH)
      const h = setup(check, { text: LONG, repo: 'cairnea/KinDash' })
      let out: unknown = 'unset'
      await act(async () => { out = await h.result.current.ensureChecked() })
      expect(out).toBeNull()
    })

    it('gives up with null after submitWaitMs', async () => {
      const d = deferred<RepoCheckResponse | null>()
      const check = vi.fn<Check>().mockReturnValue(d.promise)
      const h = setup(check, { text: LONG })
      let out: unknown = 'unset'
      const p = h.result.current.ensureChecked().then((r) => { out = r })
      await act(async () => { await vi.advanceTimersByTimeAsync(2999) })
      expect(out).toBe('unset')
      await act(async () => { await vi.advanceTimersByTimeAsync(1); await p })
      expect(out).toBeNull()
    })

    it('returns null when the check throws (fail-open)', async () => {
      const check = vi.fn<Check>().mockRejectedValue(new Error('boom'))
      const h = setup(check, { text: LONG })
      let out: unknown = 'unset'
      await act(async () => { out = await h.result.current.ensureChecked() })
      expect(out).toBeNull()
      expect(h.result.current.suggestion).toBeNull()
    })

    it('returns null below minChars without calling', async () => {
      const check = vi.fn<Check>().mockResolvedValue(KINDASH)
      const h = setup(check, { text: 'short' })
      let out: unknown = 'unset'
      await act(async () => { out = await h.result.current.ensureChecked() })
      expect(out).toBeNull()
      expect(check).not.toHaveBeenCalled()
    })
  })

  it('never calls when disabled', async () => {
    const check = vi.fn<Check>().mockResolvedValue(KINDASH)
    const h = setup(check, { text: LONG, enabled: false })
    await idle(5000)
    let out: unknown = 'unset'
    await act(async () => { out = await h.result.current.ensureChecked() })
    expect(out).toBeNull()
    expect(check).not.toHaveBeenCalled()
    expect(h.result.current.suggestion).toBeNull()
  })

  it('honours custom debounceMs / minChars', async () => {
    const check = vi.fn<Check>().mockResolvedValue(KINDASH)
    renderHook(() => useRepoSuggestion({
      text: 'fix KinDash', repo: '', enabled: true, check, debounceMs: 100, minChars: 5,
    }))
    await idle(99)
    expect(check).not.toHaveBeenCalled()
    await idle(1)
    expect(check).toHaveBeenCalledTimes(1)
  })
})
