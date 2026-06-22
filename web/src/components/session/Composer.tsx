import { useRef, useState, useCallback, useEffect } from 'react'
import { Paperclip, X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { useImageAttachments } from './useImageAttachments'
import { AttachmentChips } from './AttachmentChips'
import { CommandPalette, type CommandPaletteHandle } from './CommandPalette'
import type { Command } from '@/session/events'

interface Props {
  status: { state: string; count: number }
  mode: string
  onPrompt: (text: string, attachmentIds: string[]) => void
  onInterrupt: () => void
  onSetMode: (mode: string) => void
  queued?: string[]
  onCancelQueued?: (index: number) => void
  chatArmed?: boolean
  commands?: Command[]
  onCommand?: (name: string, args: string) => void
  /** Auto-focus the textarea when this changes / on mount (mirrors DispatchBar). */
  autoFocusKey?: unknown
}

export function Composer({ status, mode, onPrompt, onInterrupt, onSetMode, queued, onCancelQueued, chatArmed, commands = [], onCommand, autoFocusKey }: Props) {
  const [text, setText] = useState('')
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const paletteRef = useRef<CommandPaletteHandle>(null)
  const isWorking = status.state === 'working'
  const att = useImageAttachments()
  const slash = text.startsWith('/') && !text.includes('\n')
  const afterSlash = slash ? text.slice(1) : ''
  // The palette is the keyboard owner only while the user is still typing the
  // command NAME (no space yet). Once a space is typed they're entering args, so
  // Enter must submit the command (with its args) instead of re-selecting from
  // the palette — otherwise the trailing text is silently dropped.
  const pickingCommand = slash && !/\s/.test(afterSlash)
  const slashQuery = afterSlash.split(/\s+/)[0] ?? ''

  // Focus the prompt on mount (and when the active session changes) so the user
  // can start typing immediately on entering a session.
  useEffect(() => {
    textareaRef.current?.focus()
  }, [autoFocusKey])

  // Auto-grow textarea: reset height then set to scrollHeight (capped at 8 rows ~192px)
  function handleChange(e: React.ChangeEvent<HTMLTextAreaElement>) {
    setText(e.target.value)
    const el = e.target
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 192)}px`
  }

  const runCommandFromText = useCallback((raw: string): boolean => {
    if (!raw.startsWith('/') || raw.includes('\n')) return false
    const m = raw.slice(1).match(/^(\S+)\s*(.*)$/)
    if (!m) return false
    const known = commands.find((c) => c.name === m[1])
    if (!known) return false
    onCommand?.(known.name, m[2] ?? '')
    return true
  }, [commands, onCommand])

  const submit = useCallback(() => {
    const trimmed = text.trim()
    const refs = att.refs()
    if (!trimmed && refs.length === 0) return
    if (refs.length === 0 && runCommandFromText(trimmed)) {
      setText(''); if (textareaRef.current) textareaRef.current.style.height = 'auto'; return
    }
    onPrompt(trimmed, refs)
    setText(''); att.clear()
    // Reset height
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
    }
  }, [text, onPrompt, att, runCommandFromText])

  function handlePaste(e: React.ClipboardEvent) {
    const files = Array.from(e.clipboardData?.files || [])
    if (files.length) { e.preventDefault(); void att.addFiles(files) }
  }

  function handleDrop(e: React.DragEvent) {
    const files = Array.from(e.dataTransfer?.files || [])
    if (files.length) { e.preventDefault(); void att.addFiles(files) }
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (pickingCommand && paletteRef.current?.handleKeyDown(e)) return
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
    <div
      className={cn(
        'flex flex-col gap-2 px-3 py-2',
        'border-t border-gray-200 dark:border-gray-700',
        'bg-white dark:bg-gray-900',
      )}
      onDrop={handleDrop}
      onDragOver={(e) => e.preventDefault()}
    >
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
                <X size={16} aria-hidden="true" />
              </button>
            </div>
          ))}
        </div>
      )}

      <AttachmentChips attachments={att.attachments} onRemove={att.remove} />

      <div className="relative flex items-end gap-2">
        {pickingCommand && (
          <CommandPalette
            ref={paletteRef}
            commands={commands}
            query={slashQuery}
            onSelect={(c) => { if (c.argumentHint) { setText(`/${c.name} `) } else { onCommand?.(c.name, ''); setText('') } }}
            onComplete={(c) => setText(`/${c.name} `)}
            onDismiss={() => { /* keep text; palette hides when slash becomes false */ }}
          />
        )}
        <textarea
          ref={textareaRef}
          aria-label="Message"
          value={text}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          rows={1}
          placeholder={chatArmed ? 'Reply in chat…' : 'Send a message…'}
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

        <input
          ref={fileRef}
          type="file"
          accept="image/png,image/jpeg,image/gif,image/webp"
          multiple
          className="hidden"
          onChange={(e) => { if (e.target.files) void att.addFiles(e.target.files); e.target.value = '' }}
        />
        <button
          type="button"
          aria-label="Attach image"
          onClick={() => fileRef.current?.click()}
          className={cn(
            'flex-shrink-0 rounded-md px-3 py-2 text-sm min-h-[44px]',
            'border border-gray-300 dark:border-gray-600',
            'text-gray-600 dark:text-gray-300',
            'hover:bg-gray-100 dark:hover:bg-gray-800',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
          )}
        >
          <Paperclip size={16} aria-hidden="true" />
        </button>

        {chatArmed ? (
          <button
            type="button"
            aria-label="Send reply"
            title="Answers the pending question and continues in chat"
            onClick={submit}
            className={cn(
              'flex-shrink-0 rounded-md px-3 py-2 text-sm font-medium min-h-[44px]',
              'bg-blue-600 text-white hover:bg-blue-700',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
            )}
          >
            Reply
          </button>
        ) : isWorking ? (
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
