import { Toaster } from 'sonner'

/**
 * Where a toast may sit on a phone. Sonner's own `max-width: 600px` media query
 * turns any position into a full-width strip at `--mobile-offset-top`; with
 * the default 16px that strip started below the safe area and landed on the
 * page header / session status bar — the content a phone has least of.
 *
 * `env(safe-area-inset-top)` puts the strip exactly over the `TopBar` band
 * (brand mark + Assistant/Help), which is the one band on a phone that holds
 * nothing the user is reading. The side insets match the TopBar's `px-3`
 * rhythm closely enough that the toast reads as a replacement for that chrome
 * rather than as a card dropped onto the page.
 */
const TOASTER_MOBILE_OFFSET = {
  top: 'env(safe-area-inset-top)',
  left: '0.5rem',
  right: '0.5rem',
} as const

/**
 * The app's one `<Toaster>`. Mounted by `main.tsx` OUTSIDE `<ErrorBoundary>`
 * (a crash must not unmount the surface that reports it) and outside the
 * router (no toast navigates). Keeping the props here rather than inline in
 * `main.tsx` is what lets a test pin them.
 *
 * Desktop: `top-right`, over the page header's action cluster and nothing
 * else; the previous `top-center` sat on the page title. `closeButton` gives
 * mouse users a dismiss that does not depend on the 4 s timer; touch users
 * swipe.
 */
export function AppToaster() {
  return (
    <Toaster
      position="top-right"
      richColors
      closeButton
      theme="system"
      mobileOffset={TOASTER_MOBILE_OFFSET}
    />
  )
}
