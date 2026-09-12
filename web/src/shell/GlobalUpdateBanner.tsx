import { useVersionWatch } from '@/hooks/useVersionWatch'
import { useAutoReload } from '@/hooks/useAutoReload'
import { NewVersionBanner } from './NewVersionBanner'

/**
 * App-level glue: detect a newer deploy, auto-reload when safe, and otherwise surface a banner
 * with a manual Reload. Renders nothing until an update is available. Mounted once in App above
 * both the desktop and mobile layouts so it appears on every route.
 */
export function GlobalUpdateBanner() {
  const updateAvailable = useVersionWatch()
  useAutoReload(updateAvailable)
  if (!updateAvailable) return null
  return <NewVersionBanner onReload={() => location.reload()} />
}
