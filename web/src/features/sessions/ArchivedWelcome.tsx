/**
 * ArchivedWelcome — `/sessions/archived` with no archived transcript open.
 *
 * The archived LIST is not a page any more: it is the archived scope of the
 * sessions list column (`ArchivedScopeBody`), so on desktop it sits beside this
 * pane and on phone it replaces it. What is left for the page slot is the same
 * empty state `WelcomePane` gives the live scope — same `EmptyPane` layout, one
 * different sentence. Rendering the list here as well would mount it twice on
 * desktop, with two select-all headers over one selection.
 */
import { EmptyPane } from './EmptyPane'

export function ArchivedWelcome() {
  return <EmptyPane>Select an archived session from the left to reread its transcript.</EmptyPane>
}
