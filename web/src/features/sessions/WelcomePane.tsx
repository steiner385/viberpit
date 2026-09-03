/**
 * WelcomePane — `/sessions` with no session open.
 *
 * Moved verbatim out of `components/DesktopShell.tsx` (Task 17) when that shell
 * was deleted. `shell/routes.tsx` has rendered it since Task 15; this is the
 * same empty state, now living beside the page that shows it rather than inside
 * a shell that no longer exists.
 */
export function WelcomePane() {
  return (
    <div className="flex flex-col items-center justify-center h-full gap-2 text-center px-6">
      <p className="text-ink-muted text-sm">
        Select a session from the left, or type below to start a new one.
      </p>
    </div>
  )
}
