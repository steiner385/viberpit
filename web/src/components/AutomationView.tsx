import { useEffect } from 'react'
import { useAutomations } from '@/api/hooks'
import { setSessionsViewing } from '@/api/sessionsViewing'
import { AutomationCard } from './AutomationCard'

export function AutomationView() {
  const { data, isLoading } = useAutomations()
  // No sessions-summary work needed here; leave the sessions-viewing flag off.
  useEffect(() => { setSessionsViewing(false) }, [])
  if (isLoading) return <p className="text-sm text-ink-muted p-4">Loading…</p>
  return (
    <div className="space-y-3">
      {(data ?? []).map((a) => (
        <AutomationCard key={a.id} automation={a} />
      ))}
    </div>
  )
}

export default AutomationView
