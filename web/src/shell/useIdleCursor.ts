/**
 * Hide the pointer on an unattended display (shell spec §5): after `idleMs`
 * without pointer or keyboard input, `<html>` gains `data-idle`, which
 * `index.css` turns into `cursor: none`; any input clears it. Only the
 * attribute lives here — the cursor rule is CSS, so a stuck attribute is the
 * worst failure (visible, harmless), never a hidden cursor on a desk.
 */
import { useEffect } from 'react'

export const IDLE_ATTR = 'data-idle'
const WAKE_EVENTS = ['mousemove', 'keydown', 'pointerdown'] as const

export function useIdleCursor(enabled: boolean, idleMs = 5000): void {
  useEffect(() => {
    if (!enabled) return
    const root = document.documentElement
    let timer: ReturnType<typeof setTimeout> | null = null
    const arm = () => {
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => root.setAttribute(IDLE_ATTR, ''), idleMs)
    }
    const wake = () => { root.removeAttribute(IDLE_ATTR); arm() }
    WAKE_EVENTS.forEach((e) => window.addEventListener(e, wake))
    arm()
    return () => {
      WAKE_EVENTS.forEach((e) => window.removeEventListener(e, wake))
      if (timer) clearTimeout(timer)
      root.removeAttribute(IDLE_ATTR)
    }
  }, [enabled, idleMs])
}
