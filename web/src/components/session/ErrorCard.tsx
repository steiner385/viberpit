import { cn } from '@/lib/utils'

interface Props {
  code: string
  message: string
  resetsAt?: string | null
}

/**
 * Inline transcript card for an error anchored to a turn. Mirrors the top-level
 * error banner's styling but lives in the message timeline, so the failure stays
 * visible in place even after the (single-slot) banner is replaced by a later error.
 */
export function ErrorCard({ code, message, resetsAt }: Props) {
  return (
    <div
      role="alert"
      className={cn(
        'my-1 rounded-md px-3 py-2 text-sm',
        'bg-red-50 dark:bg-red-950/40 text-red-800 dark:text-red-300',
        'border border-red-200 dark:border-red-800',
      )}
    >
      <span className="font-medium">Error</span>
      <span className="ml-1 font-mono text-xs opacity-75">({code})</span>
      <div className="mt-0.5 whitespace-pre-wrap break-words">{message}</div>
      {resetsAt && (
        <div className="mt-1 text-xs opacity-75">
          Resets at {new Date(resetsAt).toLocaleTimeString()}
        </div>
      )}
    </div>
  )
}
