/**
 * The session rail's card chrome: a `<section>` named by its `h3` title
 * (`SectionHeading`), on the shared `Card` surface. Every rail card is one.
 */
import { useId, type ReactNode } from 'react'
import { Card } from '@/ui/Card'
import { SectionHeading } from '@/ui/SectionHeading'

export function RailCard({ title, actions, children }: { title: string; actions?: ReactNode; children: ReactNode }) {
  const id = useId()
  return (
    <Card as="section" padding="sm" aria-labelledby={id} className="flex min-w-0 flex-col gap-2 text-xs">
      <SectionHeading id={id} actions={actions}>{title}</SectionHeading>
      {children}
    </Card>
  )
}

/** A rail card's label/value rows: a two-column definition list. */
export function RailFields({ children }: { children: ReactNode }) {
  return <dl className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] items-center gap-x-3 gap-y-1">{children}</dl>
}

export function RailField({ label, children, testId }: { label: string; children: ReactNode; testId?: string }) {
  return (
    <>
      <dt className="text-ink-muted">{label}</dt>
      <dd className="min-w-0 break-words text-ink" data-testid={testId}>{children}</dd>
    </>
  )
}

/** The muted line a card shows when it has nothing to list. */
export function RailEmpty({ children }: { children: ReactNode }) {
  return <p className="text-ink-muted">{children}</p>
}
