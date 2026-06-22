import { useRef, useState, useCallback, useEffect } from 'react'
import { Paperclip, X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { useImageAttachments } from './useImageAttachments'
import { AttachmentChips } from './AttachmentChips'
import { CommandPalette, type CommandPaletteHandle } from './CommandPalette'
import { MicButton } from './MicButton'
import { useDictationField } from '@/lib/useDictationField'
import type { Command } from '@/session/events'
import { statusTier } from '@/session/statusTier'
import { caretLineEdges } from '@/lib/caretLineEdges'

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
  suggestions?: string[]
  history?: string[]
}

export function Composer({ status, mode, onPrompt, onInterrupt, onSetMode, queued, onCancelQueued, chatArmed, commands = [], onCommand, autoFocusKey, suggestions, history = [] }: Props) {
  const [text, setText] = useState('')
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const paletteRef = useRef<CommandPaletteHandle>(null)
  const isWorking = statusTier(status.state) === 'busy'
  const att = useImageAttachments()
  const voice = useDictationField({ value: text, setValue: setText, textareaRef })
  const slash = text.startsWith('/') && !text.includes('\n')
  const afterSlash = slash ? text.slice(1) : ''
  // The palette is the keyboard owner only while the user is still typing the
  // command NAME (no space yet). Once a space is typed they're entering args, so
  // Enter must submit the command (with its args) instead of re-selecting from
  // the palette — otherwise the trailing text is silently dropped.
  const pickingCommand = slash && !/\s/.test(afterSlash)
  const slashQuery = afterSlash.split(/\s+/)[0] ?? ''

  // History recall: histPos 0 = live draft; N = N-th entry counting back from newest.
  // Stored in a ref (not state) because it is only read in event handlers and
  // does not directly drive any rendered output — setText drives the display.
  const histPosRef = useRef(0)
  const draftRef = useRef('')
  const caretToEndRef = useRef(false)

  // Focus the prompt on mount (and when the active session changes) so the user
  // can start typing immediately on entering a session. Also reset recall state.
  useEffect(() => {
    textareaRef.current?.focus()
    histPosRef.current = 0
    draftRef.current = ''
  }, [autoFocusKey])

  // After a programmatic recall, grow the box and place the caret at the end.
  useEffect(() => {
    if (!caretToEndRef.current) return
    caretToEndRef.current = false
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 192)}px`
    const end = el.value.length
    el.setSelectionRange(end, end)
  }, [text])

  const recallTo = useCallback((pos: number, value: string) => {
    caretToEndRef.current = true
    histPosRef.current = pos
    setText(value)
  }, [])

  // Auto-grow textarea: reset height then set to scrollHeight (capped at 8 rows ~192px)
  function handleChange(e: React.ChangeEvent<HTMLTextAreaElement>) {
    if (histPosRef.current !== 0) histPosRef.current = 0
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
      setText(''); if (textareaRef.current) textareaRef.current.style.height = 'auto'
      histPosRef.current = 0; draftRef.current = ''; return
    }
    onPrompt(trimmed, refs)
    setText(''); att.clear()
    histPosRef.current = 0; draftRef.current = ''
    // Reset height
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
    }
  }, [text, onPrompt, att, runCommandFromText])

  const prefill = useCallback((value: string) => {
    setText(value)
    const el = textareaRef.current
    if (el) {
      el.focus()
      el.style.height = 'auto'
      el.style.height = `${Math.min(el.scrollHeight, 192)}px`
    }
  }, [])

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

    if (!pickingCommand && (e.key === 'ArrowUp' || e.key === 'ArrowDown') && history.length > 0) {
      const el = e.currentTarget
      const caret = el.selectionStart ?? 0
      const empty = text === ''
      const { atFirstLine, atLastLine } = caretLineEdges(text, caret)
      if (e.key === 'ArrowUp' && (empty || atFirstLine) && histPosRef.current < history.length) {
        if (histPosRef.current === 0) draftRef.current = text
        e.preventDefault()
        recallTo(histPosRef.current + 1, history[history.length - (histPosRef.current + 1)])
        return
      }
      if (e.key === 'ArrowDown' && (empty || atLastLine) && histPosRef.current > 0) {
        const pos = histPosRef.current - 1
        e.preventDefault()
        recallTo(pos, pos === 0 ? draftRef.current : history[history.length - pos])
        return
      }
    }

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

      {suggestions && suggestions.length > 0 && !isWorking && (
        <div data-testid="suggestion-chips" className="flex flex-wrap gap-2">
          {suggestions.map((s) => (
            <button
              key={s}
              type="button"
              aria-label={s}
              onClick={() => prefill(s)}
              className={cn(
                'inline-flex items-center justify-center rounded-full px-3 py-1 text-xs min-h-[44px]',
                'border border-blue-300 dark:border-blue-700',
                'bg-blue-50 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300',
                'hover:bg-blue-100 dark:hover:bg-blue-900/50',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
              )}
            >
              {s.length > 60 ? s.slice(0, 60) + '…' : s}
            </button>
          ))}
        </div>
      )}

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

        <MicButton
          supported={voice.supported}
          listening={voice.listening}
          error={voice.error}
          onToggle={voice.toggle}
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
