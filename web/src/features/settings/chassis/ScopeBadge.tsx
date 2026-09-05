import { Badge } from '@/ui/Badge'
import { SCOPE_LABEL, type SettingsScope } from '../scope'

const TONE = { server: 'accent', browser: 'success', device: 'warning', none: 'neutral' } as const

/** "Server" = every browser and host · "This browser" = localStorage · "This device" = push subscription. */
export function ScopeBadge({ scope }: { scope: SettingsScope }) {
  if (scope === 'none') return null
  return <Badge tone={TONE[scope]} title={`Applies to: ${SCOPE_LABEL[scope].toLowerCase()}`}>{SCOPE_LABEL[scope]}</Badge>
}
