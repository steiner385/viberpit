import { Link } from 'react-router-dom'
import { scrollBehavior } from '@/pipeline/motion'
import { capacityRowLink, type CapacityStackRow } from './capacityStack'

/** The row's label as a route link, an in-page anchor that scrolls, or plain text. */
export function CapacityRowLabel({ row, className }: { row: CapacityStackRow; className?: string }) {
  const link = capacityRowLink(row)
  if (link === null) return <span className={className}>{row.label}</span>
  if (link.kind === 'route') return <Link to={link.to} className={className}>{row.label}</Link>
  return (
    <a
      href={`#${link.targetId}`}
      className={className}
      onClick={(e) => {
        e.preventDefault()
        document.getElementById(link.targetId)?.scrollIntoView({ behavior: scrollBehavior() })
      }}
    >
      {row.label}
    </a>
  )
}
