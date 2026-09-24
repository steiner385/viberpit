/**
 * StarterChips — one-tap prompts shown above the AI assistant's empty composer.
 * Disappears the moment the thread has any content: once the assistant has replied
 * (or the user has sent anything), the starters would just be clutter above real
 * conversation.
 *
 * FollowUpChips (spec v2 §10.3) reuse the same chip row for the model-generated
 * follow-up questions (`latestSuggestions`) under the last answer. The reducer
 * already clears them on the next user message / turn start; they are additionally
 * hidden while a turn is live so a stale set never sits under a streaming answer.
 */
import { useSessionRuntimeContext } from '@/features/sessions/SessionRuntimeProvider'
import { statusTier } from '@/session/statusTier'

function PromptChips({ prompts }: { prompts: readonly string[] }) {
  const rt = useSessionRuntimeContext()
  return (
    <div className="flex flex-wrap gap-2 px-3 py-2">
      {prompts.map((text) => (
        <button
          key={text}
          type="button"
          onClick={() => rt.onPrompt(text, [])}
          className="rounded-full border border-edge px-3 py-1.5 text-left text-sm text-ink hover:bg-surface-raised"
        >
          {text}
        </button>
      ))}
    </div>
  )
}

export function StarterChips({ starters }: { starters: readonly string[] }) {
  const rt = useSessionRuntimeContext()
  if (rt.state.turns.length > 0) return null
  return <PromptChips prompts={starters} />
}

export function FollowUpChips() {
  const rt = useSessionRuntimeContext()
  const { turns, latestSuggestions, status } = rt.state
  if (turns.length === 0 || latestSuggestions.length === 0) return null
  if (statusTier(status.state) === 'busy') return null
  return <PromptChips prompts={latestSuggestions} />
}
