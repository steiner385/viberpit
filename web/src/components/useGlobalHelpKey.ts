import { useEffect } from 'react'

interface Options {
  /** When false, the listener is inert (e.g. not on the Sessions tab). */
  enabled: boolean
  onShowHelp: () => void
}

/**
 * `?` opens the help drawer from anywhere — except while typing in a field or
 * with a MODAL dialog already open. Mirrors the guards in `useGlobalListNav.ts`.
 *
 * The action stays injected rather than hard-wired to `?drawer=help`: the two
 * shells that mount this today (`Dashboard`, `DesktopShell`) still open
 * `HelpDrawer` from local state and have no `DrawerHost` to read the param, so
 * reaching for `useOpenDrawer()` in here would set a query param nothing renders
 * and silently break the `?` key in what currently ships. `AppShell` passes
 * `() => openDrawer('help')` when it takes over (Task 16); until then this hook
 * is unchanged apart from the guard below.
 *
 * The guard is `aria-modal="true"`, not any `[role="dialog"]`, because
 * `app.drawer` is a `Sheet` and above 1280px a `Sheet` is a non-modal push
 * column (`mode="push"` drops `aria-modal`). With the looser test, having the
 * assistant docked open on a desktop would make `?` dead for the whole app.
 * Every modal overlay — `Dialog`, and `Sheet` in its default overlay mode —
 * still sets `aria-modal="true"`, so nothing that used to block the key stops
 * blocking it.
 */
export function useGlobalHelpKey({ enabled, onShowHelp }: Options): void {
  useEffect(() => {
    if (!enabled) return
    function onKeyDown(e: KeyboardEvent) {
      if (e.key !== '?' && !(e.key === '/' && e.shiftKey)) return
      if (e.defaultPrevented) return
      const t = e.target as HTMLElement | null
      const tag = t?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || t?.isContentEditable) return
      if (document.querySelector('[role="dialog"][aria-modal="true"]')) return
      e.preventDefault()
      onShowHelp()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [enabled, onShowHelp])
}
