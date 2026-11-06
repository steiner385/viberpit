import { createContext, useContext } from 'react'
import type { ModelOption, Session } from '@/api/types'
import type { EffortStep } from '@/features/sessions/composer/effortChoices'
import type { SessionRuntime } from '@/session/useSessionRuntime'

/**
 * What an inline error card's recovery actions (D-19) — Switch model, Lower effort,
 * Compact — work from, provided by the LIVE session's `SessionBody`. With no
 * provider (the archived reader, the sub-agent page, the assistant drawer) the card
 * offers none of them. A context for the same reason as `rewindContext`: the card
 * sits several components deep in the transcript.
 */
export interface ErrorRecovery {
  rt: Pick<SessionRuntime, 'model' | 'onSetModel' | 'commands' | 'onPlainPrompt'>
  /** A turn is running: an effort change lands after it, never restarts it. */
  busy: boolean
  session?: Session
  /** What Lower effort does for this model catalog (`effortChoices` + `effortStep`, the
   *  prompt row's reading). A function so the lazy actions fetch the catalog and the
   *  eager side keeps the helper — importing it from the lazy chunk split it into a
   *  preloaded chunk. */
  effortStep: (models: readonly ModelOption[]) => EffortStep
}

export const ErrorRecoveryContext = createContext<ErrorRecovery | null>(null)

export function useErrorRecovery(): ErrorRecovery | null {
  return useContext(ErrorRecoveryContext)
}

/** One style for every action on the card, eager and lazy alike. */
export const ERROR_ACTION_CLASS = 'rounded px-1.5 py-0.5 text-xs font-medium text-danger hover:bg-danger/10 disabled:opacity-50 pointer-coarse:min-h-target focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent'
