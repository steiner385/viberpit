import { useRef, useState, useCallback } from 'react'
import { cn } from '@/lib/utils'

interface Props {
  status: { state: string; count: number }
  mode: string
  onPrompt: (text: string) => void
  onInterrupt: () => void
  onSetMode: (mode: string) => void
}

export function Composer({ status, mode, onPrompt, onInterrupt, onSetMode }: Props) {
  const [text, setText] = useState('')
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const isWorking = status.state === 'working'

  // Auto-grow textarea: reset height then set to scrollHeight (capped at 8 rows ~192px)
  function handleChange(e: React.ChangeEvent<HTMLTextAreaElement>) {
    setText(e.target.value)
    const el = e.target
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 192)}px`
  }

  const submit = useCallback(() => {
    const trimmed = text.trim()
    if (!trimmed) return
    onPrompt(trimmed)
    setText('')
    // Reset height
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
    }
  }, [text, onPrompt])

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      submit()
    }
    // Shift+Enter: let the browser insert a newline naturally (no action needed)
  }

  function handlePlanToggle() {
    onSetMode(mode === 'plan' ? 'default' : 'plan')
  }

  return (
    <div className={cn(
      'flex flex-col gap-2 px-3 py-2',
      'border-t border-gray-200 dark:border-gray-700',
      'bg-white dark:bg-gray-900',
    )}>
      <div className="flex items-end gap-2">
        <textarea
          ref={textareaRef}
          aria-label="Message"
          value={text}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          rows={1}
          placeholder="Send a message…"
          className={cn(
            'flex-1 resize-none rounded-md border border-gray-300 dark:border-gray-600',
            'bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100',
            'px-3 py-2 text-sm',
            'placeholder:text-gray-400 dark:placeholder:text-gray-500',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
            'min-h-[44px] overflow-y-auto',
          )}
          style={{ maxHeight: '192px' }}
        />

        {isWorking ? (
          <button
            type="button"
            aria-label="Stop current turn"
            onClick={onInterrupt}
            className={cn(
              'flex-shrink-0 rounded-md px-3 py-2 text-sm font-medium min-h-[44px]',
              'bg-red-600 text-white hover:bg-red-700',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
            )}
          >
            Stop
          </button>
        ) : (
          <button
            type="button"
            aria-label="Send message"
            onClick={submit}
            className={cn(
              'flex-shrink-0 rounded-md px-3 py-2 text-sm font-medium min-h-[44px]',
              'bg-blue-600 text-white hover:bg-blue-700',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
            )}
          >
            Send
          </button>
        )}
      </div>

      {/* Plan mode toggle */}
      <div className="flex items-center gap-2">
        <button
          type="button"
          role="switch"
          aria-checked={mode === 'plan'}
          aria-label="Plan mode"
          onClick={handlePlanToggle}
          className={cn(
            'rounded px-2 py-1 text-xs font-medium min-h-[44px]',
            'border',
            mode === 'plan'
              ? 'bg-blue-100 dark:bg-blue-900/40 border-blue-400 text-blue-700 dark:text-blue-300'
              : 'bg-white dark:bg-gray-800 border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-400',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
          )}
        >
          Plan mode
        </button>
      </div>
    </div>
  )
}
