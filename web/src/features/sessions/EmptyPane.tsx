/**
 * The Sessions section's empty page state — one centred sentence, one layout.
 *
 * `WelcomePane` (`/sessions`) and `ArchivedWelcome` (`/sessions/archived`) are
 * the same pane with different copy, and they were the same class strings
 * written out twice. The layout lives here so a change to it cannot land on one
 * scope and not the other; each caller supplies only its sentence.
 *
 * It also meets the page contract (docs/ACCESSIBILITY.md §3) for the routes it
 * IS the page of: exactly one `h1` (visually hidden — the sentence is the
 * visible content, the heading is the route's name for a screen reader) and a
 * `document.title` claimed through the same hook `PageHeader` uses. The
 * 2026-09-29 re-critique found every Sessions route announcing only "ViberPit".
 */
import type { ReactNode } from 'react'
import { usePageTitle } from '@/shell/usePageTitle'

export function EmptyPane({ title, children }: { title: string; children: ReactNode }) {
  usePageTitle(title)
  return (
    <div className="flex flex-col items-center justify-center h-full gap-2 text-center px-6" data-page-variant="workspace">
      <h1 className="sr-only">{title}</h1>
      <p className="text-ink-muted text-sm">{children}</p>
    </div>
  )
}
