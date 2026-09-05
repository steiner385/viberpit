/**
 * StarterChips — one-tap prompts shown above the AI assistant's empty composer.
 * Disappears the moment the thread has any content: once the assistant has replied
 * (or the user has sent anything), the starters would just be clutter above real
 * conversation.
 */
import { useSessionRuntimeContext } from '@/components/session/SessionRuntimeProvider'

export function StarterChips({ starters }: { starters: readonly string[] }) {
  const rt = useSessionRuntimeContext()
  if (rt.state.turns.length > 0) return null
  return (
    <div className="flex flex-wrap gap-2 px-3 py-2">
      {starters.map((text) => (
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
