import { useSearchParamState } from '@/lib/useSearchParamState'

/** `?tab=` as state (page-migration §2: tabs are URL state). Thin wrapper over
 *  the generic `useSearchParamState` — kept so existing callers and tests need
 *  no changes. */
export const useTabParam = <T extends string>(allowed: readonly T[], fallback: T): [T, (t: T) => void] =>
  useSearchParamState('tab', allowed, fallback)
