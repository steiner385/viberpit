import { Mic } from 'lucide-react'
import { cn } from '@/lib/utils'

interface Props {
  supported: boolean
  listening: boolean
  error: string | null
  onToggle: () => void
}

export function MicButton({ supported, listening, error, onToggle }: Props) {
  if (!supported) return null
  return (
    <button
      type="button"
      aria-label={listening ? 'Stop dictation' : 'Start dictation'}
      aria-pressed={listening}
      title={error ? `Voice error: ${error}` : listening ? 'Stop dictation' : 'Dictate'}
      onClick={onToggle}
      className={cn(
        'flex-shrink-0 rounded-md px-3 py-2 text-sm min-h-target border',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
        listening
          ? 'border-danger bg-danger-wash text-danger animate-pulse'
          : 'border-edge-strong text-ink-muted hover:bg-surface-raised',
        error && !listening && 'border-warning text-warning',
      )}
    >
      <Mic size={16} aria-hidden="true" />
    </button>
  )
}
