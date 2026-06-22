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
        'flex-shrink-0 rounded-md px-3 py-2 text-sm min-h-[44px] border',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
        listening
          ? 'border-red-400 bg-red-50 dark:bg-red-900/30 text-red-600 dark:text-red-400 animate-pulse'
          : 'border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800',
        error && !listening && 'border-amber-400 text-amber-600 dark:text-amber-400',
      )}
    >
      <Mic size={16} aria-hidden="true" />
    </button>
  )
}
