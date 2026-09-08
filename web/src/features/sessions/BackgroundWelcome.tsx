/**
 * BackgroundWelcome — `/sessions/background` with no session open.
 *
 * The background LIST is the background scope of the sessions list column
 * (`BackgroundScopeBody`), so on desktop it sits beside this pane and on phone
 * it replaces it. What is left for the page slot is the same empty state
 * `WelcomePane` and `ArchivedWelcome` give their scopes — one `EmptyPane`, one
 * different sentence. It also says what the scope IS, because nothing else on
 * the page does: a row that needs a human is not here but in Active.
 */
import { EmptyPane } from './EmptyPane'

export function BackgroundWelcome() {
  return (
    <EmptyPane>
      Routine maintenance sessions ViberPit runs on its own. One that needs you
      moves to Active until it is resolved; select a session from the left to
      watch it work.
    </EmptyPane>
  )
}
