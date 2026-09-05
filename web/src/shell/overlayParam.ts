import { useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'

/**
 * The shared `?param=<name>` overlay contract — the mechanics `drawerParam` is
 * built from.
 *
 * ## Why this is a module and not inlined
 *
 * The rule it holds is: a search param whose value names an overlay, read back
 * through an allow-list so an unrecognised value renders NOTHING, and written
 * with `replace` so opening an overlay is not a place Back can return to
 * (umbrella §4.4). It was extracted when `?dialog=` became a second reader of
 * that rule alongside `?drawer=`, because writing the second reader by hand
 * would have been a copy, and the first place the two would drift is the
 * `replace` — the half a reviewer cannot see in a diff.
 *
 * `?dialog=` has since gone (the New Session dialog it served was retired when
 * creating a session became `app.dock`'s job), leaving `?drawer=` as the only
 * caller. The module stays anyway: it is the reason the `replace` semantics are
 * stated once, and the next overlay param gets them for free rather than
 * re-deriving them. What is NOT kept here is a param name — those live with the
 * region that publishes them (`DRAWER_PARAM`). What lives here is only the how.
 *
 * ## Why `replace`, in one sentence
 *
 * With `push`, Back out of an overlay would step through every open and close of
 * it before it left the page.
 */

/** The validated value of `param`, or `null` when it is absent or unrecognised. */
export function useOverlayParam<N extends string>(
  param: string,
  names: readonly N[],
): N | null {
  const [searchParams] = useSearchParams()
  const raw = searchParams.get(param)
  return isOverlayName(raw, names) ? raw : null
}

/** The allow-list check, as a type guard, shared by the hook and each module's
 *  own exported `is…Name` predicate. */
export function isOverlayName<N extends string>(
  value: string | null | undefined,
  names: readonly N[],
): value is N {
  return value !== null && value !== undefined && (names as readonly string[]).includes(value)
}

/** Sets `param` to a name, preserving every other search param. Replaces. */
export function useSetOverlayParam<N extends string>(param: string): (name: N) => void {
  const [searchParams, setSearchParams] = useSearchParams()
  return useCallback(
    (name: N) => {
      const next = new URLSearchParams(searchParams)
      next.set(param, name)
      setSearchParams(next, { replace: true })
    },
    [param, searchParams, setSearchParams],
  )
}

/** Drops `param`, preserving every other search param. Replaces. */
export function useClearOverlayParam(param: string): () => void {
  const [searchParams, setSearchParams] = useSearchParams()
  return useCallback(() => {
    const next = new URLSearchParams(searchParams)
    next.delete(param)
    setSearchParams(next, { replace: true })
  }, [param, searchParams, setSearchParams])
}

/**
 * `to` with `param=value` merged into its query string — the pure half of
 * "carry an open overlay across a navigation".
 *
 * Merges rather than appends: `GlobalActions` already ships a nav target that
 * carries a query string (`/sessions?dialog=new`), so a naive
 * `` `${to}?drawer=` `` would produce two `?` and a path nothing matches.
 */
export function withOverlayParam(to: string, param: string, value: string): string {
  const [beforeHash, hash] = to.split('#')
  const [path, query = ''] = beforeHash.split('?')
  const params = new URLSearchParams(query)
  params.set(param, value)
  return `${path}?${params}${hash === undefined ? '' : `#${hash}`}`
}
