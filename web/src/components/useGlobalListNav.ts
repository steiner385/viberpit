import { useEffect } from 'react'

interface Options {
  /** When false, the listener is inert (e.g. not on the Sessions tab). */
  enabled: boolean
  onNavigate: (dir: 'up' | 'down') => void
}

/**
 * Up/Down arrows navigate the session list even when real focus is NOT in the
 * dispatch textarea — e.g. focus is parked on the "Sessions" tab button. Bails
 * whenever focus is inside an editable/form control or an open dialog, so it
 * never double-handles a key the textarea/select already consumed.
 */
export function useGlobalListNav({ enabled, onNavigate }: Options): void {
  useEffect(() => {
    if (!enabled) return
    function onKeyDown(e: KeyboardEvent) {
      if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return
      if (e.defaultPrevented) return
      const t = e.target as HTMLElement | null
      const tag = t?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || t?.isContentEditable) return
      if (document.querySelector('[role="dialog"]')) return
      e.preventDefault()
      onNavigate(e.key === 'ArrowUp' ? 'up' : 'down')
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [enabled, onNavigate])
}
