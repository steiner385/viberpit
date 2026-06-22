import { useRef, useState, useCallback } from 'react'
import { cn } from '@/lib/utils'
import { useAttachments } from './useAttachments'
import { AttachmentTray } from './AttachmentTray'

interface Props {
  status: { state: string; count: number }
  mode: string
  onPrompt: (text: string, attachmentIds: string[]) => void
  onInterrupt: () => void
  onSetMode: (mode: string) => void
  queued?: string[]
  onCancelQueued?: (index: number) => void
}

export function Composer({ status, mode, onPrompt, onInterrupt, onSetMode, queued, onCancelQueued }: Props) {
  const [text, setText] = useState('')
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const isWorking = status.state === 'working'
  const att = useAttachments()

  // Auto-grow textarea: reset height then set to scrollHeight (capped at 8 rows ~192px)
  function handleChange(e: React.ChangeEvent<HTMLTextAreaElement>) {
    setText(e.target.value)
    const el = e.target
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 192)}px`
  }

  const submit = useCallback(() => {
    const trimmed = text.trim()
    if (!trimmed && att.ids.length === 0) return
    onPrompt(trimmed, att.ids)
    setText('')
    att.clear()
    // Reset height
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
    }
  }, [text, onPrompt, att])

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

  function onPaste(e: React.ClipboardEvent) {
    const files = Array.from(e.clipboardData.files).filter((f) => f.type.startsWith('image/'))
    if (files.length) { e.preventDefault(); void att.addFiles(files) }
  }

  function onDrop(e: React.DragEvent) {
    const files = Array.from(e.dataTransfer.files).filter((f) => f.type.startsWith('image/'))
    if (files.length) { e.preventDefault(); void att.addFiles(files) }
  }

  return (
    <div className={cn(
      'flex flex-col gap-2 px-3 py-2',
      'border-t border-gray-200 dark:border-gray-700',
      'bg-white dark:bg-gray-900',
    )}>
      {/* Queued strip — shown only when there are queued items */}
      {queued && queued.length > 0 && (
        <div className="flex flex-col gap-1">
          {queued.map((item, index) => (
            <div
              key={`${index}-${item.slice(0, 20)}`}
              className={cn(
                'flex items-center justify-between gap-2 px-2 py-1 rounded',
                'bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400 text-xs',
              )}
            >
              <span className="truncate flex-1">{item.length > 80 ? item.slice(0, 80) + '…' : item}</span>
              <button
                type="button"
                aria-label="Cancel queued prompt"
                onClick={() => onCancelQueued?.(index)}
                className={cn(
                  'flex-shrink-0 flex items-center justify-center min-h-[44px] min-w-[44px]',
                  'text-gray-500 dark:text-gray-400 hover:text-red-600 dark:hover:text-red-400',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
                )}
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Attachment tray + error */}
      <AttachmentTray items={att.items} onRemove={att.remove} />
      {att.error && <p role="alert" className="text-xs text-red-600">{att.error}</p>}

      {/* Visually-hidden file input (accessible to testing-library via aria-label) */}
      <input
        ref={fileRef}
        type="file"
        accept="image/*"
        multiple
        aria-label="Attach image"
        className="sr-only"
        onChange={(e) => { void att.addFiles(Array.from(e.target.files ?? [])); e.target.value = '' }}
      />

      <div className="flex items-end gap-2">
        {/* Attach button — focusable visible trigger for the sr-only file input */}
        <button
          type="button"
          aria-label="Choose image to attach"
          onClick={() => fileRef.current?.click()}
          className="flex-shrink-0 rounded-md px-3 py-2 text-sm min-h-[44px]"
        >
          📎
        </button>

        <textarea
          ref={textareaRef}
          aria-label="Message"
          value={text}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          onPaste={onPaste}
          onDrop={onDrop}
          onDragOver={(e) => e.preventDefault()}
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
          <>
            <button
              type="button"
              aria-label="Queue message"
              title="Will send after the current turn"
              onClick={submit}
              className={cn(
                'flex-shrink-0 rounded-md px-3 py-2 text-sm font-medium min-h-[44px]',
                'bg-blue-600 text-white hover:bg-blue-700',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
              )}
            >
              Queue
            </button>
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
          </>
        ) : (
          <button
            type="button"
            aria-label="Send message"
            onClick={submit}
            disabled={att.uploading}
            className={cn(
              'flex-shrink-0 rounded-md px-3 py-2 text-sm font-medium min-h-[44px]',
              'bg-blue-600 text-white hover:bg-blue-700',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
              att.uploading && 'opacity-50 cursor-not-allowed',
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
