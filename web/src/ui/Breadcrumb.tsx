import { Link } from 'react-router-dom'
import { ChevronRight } from 'lucide-react'
import { cn } from '@/lib/utils'

export interface BreadcrumbItem {
  label: string
  /** Absent on the last item, which is the current page. */
  to?: string
}

/**
 * The graph path of the open node (roadmap spec §4 "Breadcrumb"). Every item
 * but the last is a link; the last carries `aria-current="page"`. Rendered in
 * `PageHeader.meta`, above the title, so the title stays the one `h1`.
 */
export function Breadcrumb({ items, className }: { items: BreadcrumbItem[]; className?: string }) {
  return (
    <nav aria-label="Breadcrumb" className={cn('min-w-0', className)}>
      <ol className="flex min-w-0 flex-wrap items-center gap-1 text-xs text-ink-muted">
        {items.map((item, i) => {
          const last = i === items.length - 1
          return (
            <li key={`${item.label}-${i}`} className="flex min-w-0 items-center gap-1">
              {i > 0 && <ChevronRight size={12} aria-hidden="true" className="shrink-0" />}
              {last || !item.to ? (
                <span aria-current={last ? 'page' : undefined} className={cn('truncate', last && 'text-ink')}>
                  {item.label}
                </span>
              ) : (
                <Link to={item.to} className="truncate rounded-sm hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent">
                  {item.label}
                </Link>
              )}
            </li>
          )
        })}
      </ol>
    </nav>
  )
}
