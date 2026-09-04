import * as React from 'react'
import { useBlocker } from 'react-router-dom'
import { ApiRequestError } from '@/api/client'

export interface SettingsForm<T extends object> {
  /** undefined until `initial` arrives. */
  draft: T | undefined
  set: <K extends keyof T>(key: K, value: T[K]) => void
  dirty: boolean
  saving: boolean
  /** Date.now() of the last successful save. */
  savedAt: number | null
  /** Top-of-section message; the caller renders it in a Banner. */
  error: string | null
  /** Resolves true on success. */
  save: () => Promise<boolean>
  discard: () => void
  /** The navigation blocker, for SaveBar's "Unsaved changes" dialog. */
  blocker: ReturnType<typeof useBlocker>
}

/** Shallow inequality by the keys of `initial`. */
export function isDirty<T extends object>(initial: T, draft: T): boolean {
  return (Object.keys(initial) as (keyof T)[]).some((k) => !Object.is(initial[k], draft[k]))
}

/** Pure: what the draft should be when `initial` changes. A dirty draft is never clobbered. */
export function nextDraft<T extends object>(prevInitial: T, draft: T, newInitial: T): T {
  return isDirty(prevInitial, draft) ? draft : newInitial
}

function messageOf(err: unknown): string {
  if (err instanceof ApiRequestError) {
    const d = err.body.detail
    if (typeof d === 'string') return d
    if (d && typeof d === 'object' && 'message' in d && typeof (d as { message: unknown }).message === 'string') {
      return (d as { message: string }).message
    }
    return `Request failed (${err.status})`
  }
  return err instanceof Error ? err.message : 'Save failed'
}

/**
 * The Form save model (spec §7.2): a section-local draft with explicit Save /
 * Discard, a navigation blocker while dirty, and errors that keep the input.
 *
 * Dirtiness is measured against a `baseline` — the last value known to match
 * the server — rather than against the `initial` prop directly. They are the
 * same value except in the window between a successful save and the refetch
 * that follows it: without the baseline the form would stay "dirty" after
 * saving, keeping Save enabled and the navigation blocker armed until the
 * caller's query happened to settle.
 */
export function useSettingsForm<T extends object>({ initial, save, beforeSave }: {
  initial: T | undefined
  save: (draft: T) => Promise<unknown>
  /** Optional gate run before save (e.g. a ConfirmDialog). Return false to abort. */
  beforeSave?: (draft: T, initial: T) => Promise<boolean> | boolean
}): SettingsForm<T> {
  const [draft, setDraft] = React.useState<T | undefined>(initial)
  const [baseline, setBaseline] = React.useState<T | undefined>(initial)
  const [seen, setSeen] = React.useState<T | undefined>(initial)
  const [saving, setSaving] = React.useState(false)
  const [savedAt, setSavedAt] = React.useState<number | null>(null)
  const [error, setError] = React.useState<string | null>(null)

  // Re-seed when the server value changes, unless the user has edits. Adjusted
  // during render (React's documented "state derived from a prop change"
  // pattern, as in ui/ConfirmDialog and session/ToolCallCard) rather than in an
  // effect, which would paint one frame of the stale draft first.
  if (initial !== seen) {
    setSeen(initial)
    if (initial !== undefined) {
      if (draft === undefined || baseline === undefined) {
        setDraft(initial)
        setBaseline(initial)
      } else if (seen === undefined || isDirty(seen, initial)) {
        // A new object carrying the SAME values is not a server change; ignoring
        // it keeps a post-save baseline from being reset to the stale value.
        setDraft(nextDraft(baseline, draft, initial))
        setBaseline(initial)
      }
    }
  }

  const dirty = draft !== undefined && baseline !== undefined && isDirty(baseline, draft)

  const blocker = useBlocker(dirty)

  React.useEffect(() => {
    if (!dirty) return
    const onBeforeUnload = (e: BeforeUnloadEvent) => { e.preventDefault() }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [dirty])

  const set = React.useCallback(<K extends keyof T>(key: K, value: T[K]) => {
    setDraft((d) => (d === undefined ? d : { ...d, [key]: value }))
    setError(null)
  }, [])

  const doSave = React.useCallback(async () => {
    if (draft === undefined || baseline === undefined || !dirty || saving) return false
    if (beforeSave && !(await beforeSave(draft, baseline))) return false
    setSaving(true)
    setError(null)
    try {
      await save(draft)
      setBaseline(draft)
      setSavedAt(Date.now())
      return true
    } catch (err) {
      setError(messageOf(err))
      return false
    } finally {
      setSaving(false)
    }
  }, [draft, baseline, dirty, saving, beforeSave, save])

  const discard = React.useCallback(() => {
    setDraft(baseline)
    setError(null)
  }, [baseline])

  return { draft, set, dirty, saving, savedAt, error, save: doSave, discard, blocker }
}
