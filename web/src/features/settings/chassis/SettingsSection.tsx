import * as React from 'react'
import { ScopeBadge } from './ScopeBadge'
import type { SettingsScope } from '../scope'

export interface SettingsSectionProps {
  id: string
  title: string
  description: string
  scope: SettingsScope
  badge?: React.ReactNode
  footer?: React.ReactNode
  children: React.ReactNode
}

/**
 * The chassis every section instantiates (spec §7.1): one h2 (the page's h1 is
 * "Settings" in PageHeader), a one-sentence description, the scope badge, the
 * body, and an optional footer slot that form sections fill with SaveBar.
 */
export function SettingsSection({ id, title, description, scope, badge, footer, children }: SettingsSectionProps) {
  const titleId = `settings-${id}-title`
  return (
    <section aria-labelledby={titleId} className="flex flex-col gap-5 max-w-3xl">
      <div className="flex flex-col gap-1">
        <div className="flex items-center gap-2 flex-wrap">
          <h2 id={titleId} className="text-xl font-semibold text-ink">{title}</h2>
          <ScopeBadge scope={scope} />
          {badge}
        </div>
        <p className="text-sm text-ink-muted">{description}</p>
      </div>
      <div className="flex flex-col gap-4">{children}</div>
      {footer}
    </section>
  )
}
