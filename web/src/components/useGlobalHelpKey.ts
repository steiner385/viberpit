import { useEffect } from 'react'

interface Options {
  /** When false, the listener is inert (e.g. not on the Sessions tab). */
  enabled: boolean
  onShowHelp: () => void
}

/**
 * `?` opens the help drawer from anywhere — except while typing in a field or
 * with a dialog already open. Mirrors the guards in `useGlobalListNav.ts`.
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
      if (document.querySelector('[role="dialog"]')) return
      e.preventDefault()
      onShowHelp()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [enabled, onShowHelp])
}
