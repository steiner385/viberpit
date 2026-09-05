/**
 * ArchivedWelcome — `/sessions/archived` with no archived transcript open.
 *
 * The archived LIST is not a page any more: it is the archived scope of the
 * sessions list column (`ArchivedScopeBody`), so on desktop it sits beside this
 * pane and on phone it replaces it. What is left for the page slot is the same
 * empty state `WelcomePane` gives the live scope — one sentence naming what the
 * column is for. Rendering the list here as well would mount it twice on
 * desktop, with two select-all headers over one selection.
 */
export function ArchivedWelcome() {
  return (
    <div className="flex flex-col items-center justify-center h-full gap-2 text-center px-6">
      <p className="text-ink-muted text-sm">
        Select an archived session from the left to reread its transcript.
      </p>
    </div>
  )
}
