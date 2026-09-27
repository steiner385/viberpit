import * as React from 'react'
import { cn } from '@/lib/utils'

export interface SectionHeadingProps {
  /** For `aria-labelledby` on the owning `<section>`. */
  id?: string
  /** Controls that act on the section, right-aligned on the heading row. */
  actions?: React.ReactNode
  className?: string
  children: React.ReactNode
}

/**
 * The small-caps label over a section inside a tab panel or card — the one
 * treatment for "Products", "Planned groups", "Between issues", "Links". An
 * `h3`: the page's `h1` is the `PageHeader`, the selected tab stands in for
 * the `h2`. A panel never restates its own tab's label with one of these: the
 * tab is the heading, so a heading is only for a sub-section of the panel.
 */
export function SectionHeading({ id, actions, className, children }: SectionHeadingProps) {
  const heading = (
    <h3 id={id} className={cn('text-xs font-medium uppercase text-ink-muted', !actions && className)}>
      {children}
    </h3>
  )
  if (!actions) return heading
  return (
    <div className={cn('flex items-center justify-between gap-2', className)}>
      {heading}
      {actions}
    </div>
  )
}
