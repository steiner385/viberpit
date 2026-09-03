/**
 * MainPaneSession — the open session's page body: title row, actions, transcript.
 *
 * Moved verbatim out of `components/DesktopShell.tsx` (Task 17) when that shell
 * was deleted; `shell/routes.tsx` has rendered it for `/sessions/:id` since Task
 * 15. It READS the session runtime from context rather than creating one —
 * `PageHost` owns the single `SessionRuntimeProvider` (audit F2.3), and the
 * `PromptDock` composer reads the same instance, so the transcript and the
 * prompt bar can never end up attached to two different sockets.
 *
 * It does NOT render a composer: on this shell the prompt bar is `app.dock`,
 * placed by the visibility matrix, not by the page.
 */
import { cn } from '@/lib/utils'
import { SessionGoalText } from '@/components/session/SessionGoalText'
import { SessionActions } from '@/components/session/SessionActions'
import { SessionBody } from '@/components/session/SessionBody'
import { CommandResultOverlay } from '@/components/session/CommandResultOverlay'
import { useSessionRuntimeContext } from '@/components/session/SessionRuntimeProvider'

export function MainPaneSession({ sessionId, onClosed }: { sessionId: string; onClosed: () => void }) {
  const rt = useSessionRuntimeContext()
  return (
    <div className="flex flex-col h-full min-h-0">
      <div className={cn(
        'flex items-center gap-3 px-3 py-2 flex-shrink-0',
        'bg-gray-100 dark:bg-gray-900 border-b border-gray-200 dark:border-gray-700',
      )}>
        <span className="flex-1 min-w-0 flex items-baseline gap-2">
          <span className="min-w-0 text-sm font-semibold text-gray-900 dark:text-gray-100 truncate">{rt.title}</span>
          <SessionGoalText goal={rt.goal} source={rt.goalSource} title={rt.title} />
        </span>
        <SessionActions
          sessionId={sessionId}
          sessionName={rt.title}
          onDone={onClosed}
          variant="light"
        />
      </div>
      <div className="flex-1 min-h-0 flex flex-col overflow-hidden bg-white dark:bg-gray-950">
        <SessionBody rt={rt} />
      </div>
      <CommandResultOverlay
        result={rt.commandResult}
        commands={rt.commands}
        onClose={rt.dismissCommandResult}
      />
    </div>
  )
}
