import { Toaster } from 'sonner'
import { TOAST_INSET_VARS } from './toastInsets'

/**
 * Toasts live at the top edge of the CONTENT band — below the TopBar, banners
 * and whichever page header is up, right of the rail, left of a pushed drawer
 * — never over chrome. The band and the headers publish their edges as the
 * `--toast-inset-*` variables (`shell/toastInsets.ts`); these offsets just
 * add a gap.
 *
 * The `max(…, env(safe-area-inset-top))` is the fallback: when the shell is
 * not mounted (a crash — this component sits outside `ErrorBoundary`) the
 * variables are absent and the toast returns to the safe-area viewport edge.
 * Inside a mounted shell the band already sits below the TopBar, which owns
 * the safe area, so the `max` is a no-op there.
 */
const TOP = `calc(max(var(${TOAST_INSET_VARS.top}, 0px), env(safe-area-inset-top, 0px)) + 0.5rem)`

/**
 * Desktop card. Sonner's own default is 24px from the viewport corner; we
 * keep a 1rem gap but measure it from the band's edges instead.
 */
const TOASTER_OFFSET = {
  top: TOP,
  right: `calc(var(${TOAST_INSET_VARS.right}, 0px) + 1rem)`,
  left: `calc(var(${TOAST_INSET_VARS.left}, 0px) + 1rem)`,
} as const

/**
 * Phone strip. Sonner's `max-width: 600px` media query turns any position
 * into a full-width strip at `--mobile-offset-top`, `--mobile-offset-left`
 * wide to `--mobile-offset-right`. The side insets follow the band (0 on a
 * phone, where there is no rail) plus the TopBar's `px-3`-ish rhythm.
 *
 * No `bottom`: nothing may put a toast into the NavBar / dock / bottom-sheet
 * band, which is the other place a phone keeps its controls.
 */
const TOASTER_MOBILE_OFFSET = {
  top: TOP,
  left: `calc(var(${TOAST_INSET_VARS.left}, 0px) + 0.5rem)`,
  right: `calc(var(${TOAST_INSET_VARS.right}, 0px) + 0.5rem)`,
} as const

/**
 * The app's one `<Toaster>`. Mounted by `main.tsx` OUTSIDE `<ErrorBoundary>`
 * (a crash must not unmount the surface that reports it) and outside the
 * router (no toast navigates). Keeping the props here rather than inline in
 * `main.tsx` is what lets a test pin them.
 *
 * `top-right`: the top-right of the content band is whitespace in a transcript
 * and the far end of a row in a list, and every clickable header is above it.
 * `closeButton` gives mouse users a dismiss that does not depend on the 4 s
 * timer; touch users swipe.
 */
export function AppToaster() {
  return (
    <Toaster
      position="top-right"
      richColors
      closeButton
      theme="system"
      offset={TOASTER_OFFSET}
      mobileOffset={TOASTER_MOBILE_OFFSET}
    />
  )
}
