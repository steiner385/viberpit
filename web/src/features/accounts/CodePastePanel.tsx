/**
 * CodePastePanel — the claude.ai code paste: open the authorize link in your own
 * browser, approve, then paste the code it shows back here. One panel for every
 * code-paste sign-in (an existing account's re-auth or browser login, and a new
 * account's first sign-in in the Add dialog), so the link, copy and paste markup
 * exists once.
 */
import { useState } from 'react'
import { cn } from '@/lib/utils'

interface CodePastePanelProps {
  authUrl: string
  /** What the operator should do, in the flow's own words. */
  instructions: string
  /** Accessible name of the code input (names the account). */
  inputLabel: string
  /** Called with the trimmed code; `onAccepted` clears the input once the server takes it. */
  onSubmit: (code: string, onAccepted: () => void) => void
}

export function CodePastePanel({ authUrl, instructions, inputLabel, onSubmit }: CodePastePanelProps) {
  const [code, setCode] = useState('')

  return (
    <div className="flex flex-col gap-2 mt-1">
      <p className="text-xs text-warning">{instructions}</p>
      <div className="flex items-center gap-2 min-w-0">
        <a
          href={authUrl}
          target="_blank"
          rel="noopener noreferrer"
          className={cn(
            'text-xs text-accent underline truncate min-w-0 flex-1',
          )}
        >
          {authUrl}
        </a>
        <button
          type="button"
          className={cn(
            'rounded px-2 py-1 text-xs flex-shrink-0',
            'border border-edge-strong',
            'text-ink',
            'hover:bg-surface-raised',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
          )}
          onClick={() => { void navigator.clipboard.writeText(authUrl) }}
        >
          Copy
        </button>
      </div>
      <div className="flex items-center gap-2">
        <input
          type="text"
          value={code}
          onChange={(e) => setCode(e.target.value)}
          aria-label={inputLabel}
          placeholder="Paste authorization code…"
          className={cn(
            'flex-1 min-w-0 rounded border border-edge-strong',
            'bg-surface text-ink',
            'px-2 py-1 text-xs',
            'focus:outline-none focus:ring-2 focus:ring-accent',
          )}
        />
        <button
          type="button"
          className={cn(
            'rounded px-2 py-1 text-xs font-medium flex-shrink-0',
            'bg-accent text-accent-ink hover:bg-accent/90',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
            'disabled:opacity-40',
          )}
          disabled={!code.trim()}
          onClick={() => onSubmit(code.trim(), () => setCode(''))}
        >
          Submit code
        </button>
      </div>
    </div>
  )
}
