// The four-number band at the top of a roadmap detail: how much is in flight,
// how much is stuck, how much wants a human. A description list rather than a
// grid of divs, so a screen reader reads "In flight, 3" as one pair.
import { TONE_TEXT, type Tone } from '@/ui/Badge'
import { cn } from '@/lib/utils'

export interface StatTile {
  label: string
  value: number
  /** Colours the number only; an untoned tile stays in primary ink. */
  tone?: Tone
  onClick?: () => void
}

export function StatTiles({ tiles }: { tiles: StatTile[] }) {
  return (
    // Two columns on a phone, four from `sm` up: four 2xl numerals do not fit
    // in 390px without the page scrolling sideways (page contract §2.3).
    <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
      {tiles.map((t) => (
        <div key={t.label} className="relative rounded-md border border-edge bg-surface p-3">
          <dt className="text-xs text-ink-muted">
            {t.onClick
              ? <button type="button" onClick={t.onClick} aria-label={`${t.label}: ${t.value}`} className="after:absolute after:inset-0 after:rounded-md hover:text-accent focus-visible:outline-none focus-visible:after:ring-2 focus-visible:after:ring-accent">{t.label}</button>
              : t.label}
          </dt>
          <dd className={cn('text-2xl font-semibold tabular-nums', t.tone ? TONE_TEXT[t.tone] : 'text-ink')}>
            {t.value}
          </dd>
        </div>
      ))}
    </dl>
  )
}
