import { useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'

/** `?tab=` as state (page-migration §2: tabs are URL state). Replace, not push. */
export function useTabParam<T extends string>(allowed: readonly T[], fallback: T): [T, (t: T) => void] {
  const [params, setParams] = useSearchParams()
  const raw = params.get('tab')
  const tab = (allowed as readonly string[]).includes(raw ?? '') ? (raw as T) : fallback
  const setTab = useCallback((t: T) => {
    setParams((prev) => { const next = new URLSearchParams(prev); next.set('tab', t); return next }, { replace: true })
  }, [setParams])
  return [tab, setTab]
}
