import { useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'

/** `?<name>=` as state (page-migration §2: tabs/filters are URL state). Replace,
 *  not push — flipping a tab or a filter must not grow the Back stack. Generic
 *  over `name` so every `?x=` param (tab, window, scope, …) shares one hook
 *  instead of a copy per param name; `useTabParam` is the `'tab'` specialization. */
export function useSearchParamState<T extends string>(
  name: string, allowed: readonly T[], fallback: T,
): [T, (t: T) => void] {
  const [params, setParams] = useSearchParams()
  const raw = params.get(name)
  const value = (allowed as readonly string[]).includes(raw ?? '') ? (raw as T) : fallback
  const setValue = useCallback((t: T) => {
    setParams((prev) => { const next = new URLSearchParams(prev); next.set(name, t); return next }, { replace: true })
  }, [setParams, name])
  return [value, setValue]
}
