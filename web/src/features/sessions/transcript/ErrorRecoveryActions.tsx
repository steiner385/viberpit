/**
 * An error card's recovery actions beyond Rewind & retry (D-19), keyed off the
 * server's error `kind`. Lazy (`ErrorCard`): only a failed turn ever loads it.
 *
 * - Switch model (rate limit, overloaded, model unavailable) opens the prompt row's
 *   model picker — the same opener as Alt+Shift+M. It never picks a model itself.
 * - Lower effort (rate limit, overloaded) steps one level down the efforts the
 *   prompt row's picker offers (`ErrorRecovery.lowerEffort`), through the same switch
 *   the picker uses. Absent at the floor, with no efforts, or when the level in force
 *   is unknown.
 * - Compact (context too long) sends `/compact`, where the command exists.
 */
import { ChevronsDown } from 'lucide-react'
import { useModels } from '@/api/hooks'
import { cn } from '@/lib/utils'
import type { ErrorKind } from '@/session/events'
import { usePromptRowMenusHandle } from '@/shell/sessionGestures'
import type { ErrorRecovery } from './errorRecoveryContext'

/** `className`: the card's action style (`ERROR_ACTION_CLASS`), handed in — importing
 *  any value from the eager side split it, or a vendor chunk, off the entry path. */
export default function ErrorRecoveryActions({ kind, recovery, className }: {
  kind: ErrorKind; recovery: ErrorRecovery; className: string
}) {
  const menus = usePromptRowMenusHandle()
  const { rt, session } = recovery
  const { data: models = [] } = useModels(session?.engine, session?.accountName)
  const limited = kind === 'rate_limit' || kind === 'overloaded'
  const lower = limited ? recovery.lowerEffort(models) : null
  return (
    <>
      {(limited || kind === 'model_unavailable') && (
        // The live session's dock composer always holds the prompt row.
        <button type="button" className={className} onClick={() => menus.current?.openModel()}>Switch model</button>
      )}
      {lower !== null && (
        <button
          type="button"
          className={cn(className, 'inline-flex items-center gap-0.5')}
          title={`Set reasoning effort to ${lower}`}
          onClick={() => rt.onSetModel(rt.model ?? 'default', recovery.busy ? 'after_turn' : 'now', lower)}
        >
          <ChevronsDown size={12} aria-hidden="true" className="inline" /> Lower effort
        </button>
      )}
      {kind === 'context_too_long' && rt.commands.some((c) => c.name === 'compact') && (
        <button
          type="button"
          className={className}
          title="Summarize the conversation to free context, then retry"
          // The Context card's Compact now: `/compact` as a plain prompt.
          onClick={() => rt.onPlainPrompt('/compact', [])}
        >
          Compact
        </button>
      )}
    </>
  )
}
