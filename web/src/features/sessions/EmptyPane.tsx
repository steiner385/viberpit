/**
 * The Sessions section's empty page state — one centred sentence, one layout.
 *
 * `WelcomePane` (`/sessions`) and `ArchivedWelcome` (`/sessions/archived`) are
 * the same pane with different copy, and they were the same class strings
 * written out twice. The layout lives here so a change to it cannot land on one
 * scope and not the other; each caller supplies only its sentence.
 */
import type { ReactNode } from 'react'

export function EmptyPane({ children }: { children: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center h-full gap-2 text-center px-6">
      <p className="text-ink-muted text-sm">{children}</p>
    </div>
  )
}
