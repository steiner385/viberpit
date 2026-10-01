import { Link, useNavigate } from 'react-router-dom'
import { scrollBehavior } from '@/pipeline/motion'
import { capacityPath } from '../deliveryNavState'
import { capacityRowLink, type CapacityStackRow } from './capacityStack'

/**
 * The row's label as a route link, an in-page anchor that scrolls, or plain text.
 *
 * An anchor scrolls to its target when the target is on screen — the pane
 * beside the list on desktop (spec 2026-09-20 D10, F2). Below desktop the
 * pane is not mounted at all while the list has the band, so the same anchor
 * used to do nothing (2026-10-01 Delivery re-critique, P0); now it opens the
 * pane as an item with the hash, and `CapacityDetail` scrolls to it on mount.
 * Decided by the DOM, not the viewport mode: whether the target exists is the
 * fact that matters, and it keeps `useViewport` the shell's business.
 */
export function CapacityRowLabel({ row, className }: { row: CapacityStackRow; className?: string }) {
  const navigate = useNavigate()
  const link = capacityRowLink(row)
  if (link === null) return <span className={className}>{row.label}</span>
  if (link.kind === 'route') return <Link to={link.to} className={className}>{row.label}</Link>
  return (
    <a
      href={`#${link.targetId}`}
      className={className}
      onClick={(e) => {
        e.preventDefault()
        // "On screen" means mounted AND visible: below desktop `DeliveryPage`
        // still mounts the pane inside a hidden `PageContent`, so the target
        // exists in the DOM at 0×0 and a bare existence check scrolled nothing
        // (measured on the branch dev server). `checkVisibility` is absent in
        // jsdom, where mounted is all there is.
        const target = document.getElementById(link.targetId)
        const visible = target !== null && (target.checkVisibility?.() ?? true)
        if (target && visible) target.scrollIntoView({ behavior: scrollBehavior() })
        else navigate(capacityPath(link.targetId))
      }}
    >
      {row.label}
    </a>
  )
}
