// Kiosk repo auto-cycle (plotroom replica spec §kiosk; shell spec §5): advance
// the focused repo every cycleSeconds, paused while the tab is hidden — mirrors
// plotroom App.tsx:84-91 kiosk cycling (which ticks a nonce and re-pins
// Delivery; here we rotate the focused pipeline so PipelineView's focused-first
// ordering walks each repo in turn). Page-local by design: the shell's attract
// cycle (`shell/useAttractCycle`) rotates PAGES; this rotates repos WITHIN the
// Pipeline page. Formerly `pipeline/useKioskCycle`.
//
// The interval is keyed on the repo list's CONTENT, and the current focus is
// read through a ref, on purpose: every dashboard frame (one per poll, ~10 s)
// produces a new `repos` array and a fresh `focus`, and an effect that depended
// on either restarted the timer on every frame — with the default 30 s cycle
// it never fired. That is how the inherited `useKioskCycle` behaved (found by
// the row-5 e2e smoke, which feeds frames faster than the cycle).
import { useEffect, useLayoutEffect, useRef } from 'react'

export function useRepoCycle(
  enabled: boolean, cycleSeconds: number, repos: readonly string[],
  focused: string | null, focus: (repo: string) => void,
) {
  const focusedRef = useRef(focused)
  const focusRef = useRef(focus)
  useLayoutEffect(() => { focusedRef.current = focused; focusRef.current = focus }, [focused, focus])
  // Repo names cannot contain a newline, so the join is a faithful identity.
  const key = repos.join('\n')
  useEffect(() => {
    const list = key ? key.split('\n') : []
    if (!enabled || list.length < 2) return
    const id = setInterval(() => {
      if (document.hidden) return // paused while the tab is not visible
      const current = focusedRef.current
      const i = current ? list.indexOf(current) : -1
      focusRef.current(list[(i + 1) % list.length]!)
    }, cycleSeconds * 1000)
    return () => clearInterval(id)
  }, [enabled, cycleSeconds, key])
}
