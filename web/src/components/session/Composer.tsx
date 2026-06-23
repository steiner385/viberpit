import { useRef, useState, useCallback, useEffect } from 'react'
import { Paperclip, X, Play, Loader2, ChevronUp } from 'lucide-react'
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
  onPrompt: (text: string, attachmentIds: string[]) => void
  onInterrupt: () => void
  queued?: string[]
  onCancelQueued?: (index: number) => void
  chatArmed?: boolean
  commands?: Command[]
  onCommand?: (name: string, args: string) => void
  /** Auto-focus the textarea when this changes / on mount (mirrors DispatchBar). */
  autoFocusKey?: unknown
  suggestions?: string[]
  history?: string[]
  onRequestSuggestions: () => void
}

export function Composer({ status, onPrompt, onInterrupt, queued, onCancelQueued, chatArmed, commands = [], onCommand, autoFocusKey, suggestions, history = [], onRequestSuggestions }: Props) {
  const [text, setText] = useState('')
  const [pendingGenerate, setPendingGenerate] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  // prevSuggestions tracks which suggestions array we last acted on for the
  // pendingGenerate→prefill flow, so we only prefill once per new suggestions array.
  const [prevSuggestions, setPrevSuggestions] = useState<string[] | undefined>(undefined)
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

  const ghost =
    !isWorking && text === '' && suggestions && suggestions.length > 0
      ? suggestions[0]
      : null

  // History recall: histPos 0 = live draft; N = N-th entry counting back from newest.
  // Stored in a ref (not state) because it is only read in event handlers and
  // does not directly drive any rendered output — setText drives the display.
  const histPosRef = useRef(0)
  const draftRef = useRef('')
  const caretToEndRef = useRef(false)
  const prevPendingGenerateRef = useRef(false)

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

  const showPlay = !isWorking && text === ''

  // When suggestions arrive while pendingGenerate is true, prefill the box for
  // confirmation (never auto-send). This uses the React "derived state during render"
  // pattern (setState during render = immediate re-render, no cascading side effect).
  // prevSuggestions guards against re-running on every re-render when nothing changed.
  if (pendingGenerate && suggestions !== prevSuggestions && suggestions && suggestions.length > 0) {
    setPrevSuggestions(suggestions)
    setText(suggestions[0])
    setPendingGenerate(false)
  }

  const play = useCallback(() => {
    if (suggestions && suggestions.length > 0) {
      onPrompt(suggestions[0], [])
      return
    }
    setPendingGenerate(true)
    onRequestSuggestions()
  }, [suggestions, onPrompt, onRequestSuggestions])

  // When the render-phase pendingGenerate→prefill block fires (pendingGenerate goes
  // true→false while suggestions just arrived), focus and auto-resize the textarea
  // so the user can immediately press Enter to confirm.
  // We detect the transition by comparing pendingGenerate to its value from the
  // previous render (tracked in prevPendingGenerateRef, updated safely in this effect).
  useEffect(() => {
    const wasPending = prevPendingGenerateRef.current
    prevPendingGenerateRef.current = pendingGenerate
    if (wasPending && !pendingGenerate && text !== '') {
      const el = textareaRef.current
      if (el) {
        el.focus()
        el.style.height = 'auto'
        el.style.height = `${Math.min(el.scrollHeight, 192)}px`
      }
    }
  })

  // Quiet-fail: if generation produces nothing within 8s, drop the spinner.
  useEffect(() => {
    if (!pendingGenerate) return
    const t = setTimeout(() => setPendingGenerate(false), 8000)
    return () => clearTimeout(t)
  }, [pendingGenerate])

  function handlePaste(e: React.ClipboardEvent) {
    const files = Array.from(e.clipboardData?.files || [])
    if (files.length) { e.preventDefault(); void att.addFiles(files) }
  }

  function handleDrop(e: React.DragEvent) {
    const files = Array.from(e.dataTransfer?.files || [])
    if (files.length) { e.preventDefault(); void att.addFiles(files) }
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (ghost && ((e.key === 'Tab' && !e.shiftKey) || e.key === 'ArrowRight')) {
      e.preventDefault()
      prefill(ghost)
      return
    }
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
        <div className="relative flex-1">
          <textarea
            ref={textareaRef}
            aria-label="Message"
            value={text}
            onChange={handleChange}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            rows={1}
            placeholder={ghost ? '' : (chatArmed ? 'Reply in chat…' : 'Send a message…')}
            className={cn(
              'w-full resize-none rounded-md border border-gray-300 dark:border-gray-600',
              'bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100',
              'px-3 py-2 text-sm',
              'placeholder:text-gray-400 dark:placeholder:text-gray-500',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
              'min-h-[44px] overflow-y-auto',
            )}
            style={{ maxHeight: '192px' }}
          />
          {ghost && (
            <div
              data-testid="ghost-suggestion"
              aria-hidden="true"
              className={cn(
                'pointer-events-none absolute inset-0 flex items-center justify-between gap-2',
                'px-3 py-2 text-sm',
              )}
            >
              <span className="truncate text-gray-400 dark:text-gray-500">{ghost}</span>
              <span className={cn(
                'flex-shrink-0 rounded border px-1 text-[10px] leading-tight',
                'border-gray-300 dark:border-gray-600 text-gray-400 dark:text-gray-500',
              )}>
                ⇥ Tab
              </span>
            </div>
          )}
        </div>

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

        {showPlay && (
          <div className="relative flex items-center">
            <button
              type="button"
              aria-label="Resume (send suggested next step)"
              title="Resume — send the suggested next step"
              onClick={play}
              disabled={pendingGenerate}
              className={cn(
                'flex-shrink-0 rounded-md px-3 py-2 text-sm font-medium min-h-[44px]',
                'bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-60',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400',
              )}
            >
              {pendingGenerate ? <Loader2 size={16} className="animate-spin" aria-hidden="true" />
                               : <Play size={16} aria-hidden="true" />}
            </button>
            {suggestions && suggestions.length >= 2 && (
              <button
                type="button"
                aria-label="More suggestions"
                aria-haspopup="menu"
                aria-expanded={menuOpen}
                onClick={() => setMenuOpen((o) => !o)}
                className={cn(
                  'flex-shrink-0 rounded-md px-1 py-2 min-h-[44px] text-emerald-700 dark:text-emerald-400',
                  'hover:bg-emerald-50 dark:hover:bg-emerald-900/20',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400',
                )}
              >
                <ChevronUp size={16} aria-hidden="true" />
              </button>
            )}
            {menuOpen && suggestions && suggestions.length >= 2 && (
              <div
                role="menu"
                className={cn(
                  'absolute bottom-full right-0 mb-1 min-w-[12rem] max-w-xs z-10',
                  'rounded-md border border-gray-200 dark:border-gray-700',
                  'bg-white dark:bg-gray-800 shadow-lg py-1',
                )}
              >
                {suggestions.slice(1).map((s, i) => (
                  <button
                    key={i}
                    type="button"
                    role="menuitem"
                    onClick={() => { setMenuOpen(false); onPrompt(s, []) }}
                    className={cn(
                      'block w-full truncate px-3 py-2 text-left text-sm',
                      'hover:bg-gray-100 dark:hover:bg-gray-700',
                    )}
                  >
                    {s}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}

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
    </div>
  )
}
