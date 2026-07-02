import { useEffect } from 'react'
import { useAutomations } from '@/api/hooks'
import { setSessionsViewing } from '@/api/sessionsViewing'
import { AutomationCard } from './AutomationCard'

export function AutomationView({ onOpenSession, onReviewSession }: {
  onOpenSession?: (id: string) => void
  onReviewSession?: (id: string) => void
} = {}) {
  const { data, isLoading } = useAutomations()
  // No sessions-summary work needed here; leave the sessions-viewing flag off.
  useEffect(() => { setSessionsViewing(false) }, [])
  if (isLoading) return <p className="text-sm text-muted-foreground p-4">Loading…</p>
  return (
    <div className="space-y-3" role="tabpanel" id="tabpanel-automation" aria-labelledby="tab-automation">
      {(data ?? []).map((a) => (
        <AutomationCard key={a.id} automation={a} onOpenSession={onOpenSession} onReviewSession={onReviewSession} />
      ))}
    </div>
  )
}

export default AutomationView
