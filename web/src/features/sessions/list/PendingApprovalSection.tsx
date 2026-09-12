// web/src/features/sessions/list/PendingApprovalSection.tsx
//
// Dashboard section listing sessions parked in the pending_approval state (launch-time
// model-approval gate). Each row shows an Approve/Deny affordance inline so the operator
// can resolve holds without opening the session. Pattern mirrors QueuedSection.
import { useState } from 'react'
import { cn } from '@/lib/utils'
import { useModelApproval } from '@/api/hooks'
import { PendingModelApprovalPanel } from '@/components/session/PendingModelApprovalPanel'
import type { Session } from '@/api/types'

interface PendingApprovalSectionProps {
  sessions: Session[]
  onOpen: (id: string) => void
}

export function PendingApprovalSection({ sessions, onOpen }: PendingApprovalSectionProps) {
  const [collapsed, setCollapsed] = useState(false)
  // One mutation instance for the whole list: `variables` names the session whose
  // request is in flight, so only THAT row's panel shows the pending state.
  const { mutate: modelApproval, isPending, variables } = useModelApproval()

  if (sessions.length === 0) return null

  return (
    <div>
      {/* Collapsible header — mirrors QueuedSection */}
      <button
        type="button"
        onClick={() => setCollapsed((c) => !c)}
        aria-expanded={!collapsed}
        aria-controls="pending-approval-section-panel"
        className={cn(
          'sticky top-0 z-raised flex w-full items-center gap-2 px-1 py-1',
          'bg-canvas/95 backdrop-blur',
          'text-xs font-medium uppercase tracking-wide text-ink-muted',
          'hover:text-ink',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-info rounded',
        )}
      >
        <span>Awaiting approval ({sessions.length})</span>
      </button>

      {!collapsed && (
        <ul id="pending-approval-section-panel" className="space-y-1 py-1">
          {sessions.map((session) => (
            <li
              key={session.id}
              className={cn(
                'rounded-lg border',
                'border-info',
                'bg-info-wash/40',
                'overflow-hidden',
              )}
            >
              {/* Open button — wraps ONLY name/label block, mirrors QueuedSection */}
              <button
                type="button"
                onClick={() => onOpen(session.id)}
                className={cn(
                  'w-full text-left px-3 py-2',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-info rounded-t-lg',
                )}
              >
                <div className="text-sm font-medium text-ink truncate">
                  {session.name}
                </div>
                {session.repo && (
                  <div className="text-xs text-ink-muted truncate">
                    {session.repo}
                  </div>
                )}
              </button>

              {/* Approve/Deny panel rendered for any hold (launch-time or mid-session) */}
              <PendingModelApprovalPanel
                model={session.pendingModel}
                reason={session.pendingModelReason}
                requestedBy={session.pendingModelRequestedBy}
                onDecision={(decision, model) => modelApproval({ id: session.id, decision, model })}
                pending={isPending && variables?.id === session.id}
              />
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
