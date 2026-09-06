/**
 * WelcomePane — `/sessions` with no session open.
 *
 * Moved verbatim out of `components/DesktopShell.tsx` (Task 17) when that shell
 * was deleted. `shell/routes.tsx` has rendered it since Task 15; this is the
 * same empty state, now living beside the page that shows it rather than inside
 * a shell that no longer exists. Its layout is `EmptyPane`, shared with the
 * archived scope's pane so the two cannot drift.
 */
import { EmptyPane } from './EmptyPane'

export function WelcomePane() {
  return <EmptyPane>Select a session from the left, or type below to start a new one.</EmptyPane>
}
