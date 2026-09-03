/**
 * The ONE answer to "is an overlay open".
 *
 * Before this there were three, two of which disagreed:
 *
 *   • `useGlobalHelpKey` queried `[role="dialog"][aria-modal="true"]`,
 *   • `useGlobalListNav` queried the looser `[role="dialog"]`, so a docked
 *     push-mode `Sheet` (still `role="dialog"`, just not modal) killed
 *     ArrowUp/ArrowDown for the whole app,
 *   • `ui/useFocusTrap` kept an authoritative stack that was module-PRIVATE,
 *     so nothing outside that file could read it.
 *
 * And none of the three saw `ui/Menu` at all — a Menu is not a focus trap and
 * renders no dialog role, yet a Menu open over the session list must still stop
 * ArrowUp reaching the list underneath it.
 *
 * SEMANTICS (ruling R5): "overlay" means a MODAL focus trap (`Dialog`, `Sheet`
 * in its default overlay mode) or an open `Menu`. A *pushed*, non-modal drawer —
 * the `Sheet` docked beside the content at >=1280 — is a REGION, not an overlay:
 * it sits next to the list rather than covering it, so it must not block global
 * shortcuts. `useFocusTrap` already encodes exactly that distinction (`Sheet`
 * activates the trap only in overlay mode), which is why registration hangs off
 * the trap rather than off "a sheet is visible".
 *
 * Deliberately a module-level counter with `useSyncExternalStore`, NOT a React
 * context: `useFocusTrap` is used by primitives that must not require a
 * provider, and a context would make every existing `Dialog` test wrap itself in
 * one. It also has to be readable from a plain `document` keydown listener,
 * which runs outside render and cannot call a hook — hence `overlayIsOpen()`.
 */
import { useSyncExternalStore } from 'react'

// Depth, not a boolean: a Dialog can open a second Dialog, and the inner one
// closing must not declare the outer one gone.
let depth = 0
const listeners = new Set<() => void>()

function emit() {
  for (const l of listeners) l()
}

/**
 * Called by `ui/useFocusTrap` (modal traps) and `ui/Menu` (open menus).
 *
 * Pairs MUST balance. A leaked push leaves the app permanently "overlay open",
 * which silently disables every shortcut in the registry with no visible
 * symptom, so both callers push and pop from the same effect's setup/cleanup.
 */
export function pushOverlay(): void {
  depth += 1
  emit()
}

/**
 * Clamped at zero. An unpaired pop must not drive the counter negative: at -1
 * the *next* real overlay's push would land on 0 and be invisible to every
 * shortcut — a leak that outlives the component that caused it.
 */
export function popOverlay(): void {
  depth = Math.max(0, depth - 1)
  emit()
}

/** Non-reactive read, for the shortcut listener — it runs outside render. */
export function overlayIsOpen(): boolean {
  return depth > 0
}

function subscribe(l: () => void): () => void {
  listeners.add(l)
  return () => {
    listeners.delete(l)
  }
}

const getSnapshot = () => depth > 0
const getServerSnapshot = () => false

/** Reactive read, for components that render differently while an overlay is up. */
export function useOverlayState(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot)
}

/** Test-only: the counter is module state and outlives a single test. */
export function resetOverlayState(): void {
  depth = 0
  emit()
}
