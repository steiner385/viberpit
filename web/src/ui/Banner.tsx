import * as React from 'react'
import { X, Info, CircleCheck, TriangleAlert, CircleAlert } from 'lucide-react'
import { cn } from '@/lib/utils'
import { IconButton } from './IconButton'

export type BannerTone = 'info' | 'success' | 'warning' | 'danger'

const TONE = {
  info:    { box: 'border-info/40 bg-info-wash text-ink',       icon: 'text-info',    Icon: Info },
  success: { box: 'border-success/40 bg-success-wash text-ink', icon: 'text-success', Icon: CircleCheck },
  warning: { box: 'border-warning/40 bg-warning-wash text-ink', icon: 'text-warning', Icon: TriangleAlert },
  danger:  { box: 'border-danger/40 bg-danger-wash text-ink',   icon: 'text-danger',  Icon: CircleAlert },
} as const

export interface BannerProps {
  tone: BannerTone
  title?: React.ReactNode
  /** Replaces the tone's default icon (pass null for none). */
  icon?: React.ReactNode | null
  actions?: React.ReactNode
  onDismiss?: () => void
  /** Live-region role. Defaults from tone: danger → alert (assertive), else status (polite).
   *  Pass explicitly wherever a call site's existing role differs from the tone default —
   *  never let a banner silently go from assertive to polite. */
  role?: 'status' | 'alert'
  className?: string
  children?: React.ReactNode
}

/**
 * The one primitive for inline banner chrome — tone, icon, dismiss. Content (title, body,
 * actions) is supplied by the caller; Banner never owns copy. See BannerStack for the single
 * region both shells mount banners into.
 */
export function Banner({ tone, title, icon, actions, onDismiss, role, className, children }: BannerProps) {
  const t = TONE[tone]
  const DefaultIcon = t.Icon
  return (
    <div
      role={role ?? (tone === 'danger' ? 'alert' : 'status')}
      className={cn('flex items-start gap-3 rounded-lg border px-4 py-3', t.box, className)}
    >
      {icon === undefined
        ? <DefaultIcon size={18} aria-hidden="true" className={cn('shrink-0 mt-0.5', t.icon)} />
        : icon && <span className={cn('shrink-0 mt-0.5', t.icon)}>{icon}</span>}
      <div className="flex-1 min-w-0">
        {title && <p className="text-sm font-medium">{title}</p>}
        {children && <div className="text-xs text-ink-muted mt-0.5">{children}</div>}
      </div>
      {actions && <div className="flex items-center gap-2 shrink-0">{actions}</div>}
      {onDismiss && (
        <IconButton aria-label="Dismiss" size="sm" onClick={onDismiss} className={t.icon}>
          <X size={14} aria-hidden="true" />
        </IconButton>
      )}
    </div>
  )
}
