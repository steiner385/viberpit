import { useRef, useState, useCallback, useEffect } from 'react'
import { Paperclip, X, Sparkles, ChevronUp } from 'lucide-react'
import { cn } from '@/lib/utils'
import { QUICK_REPLIES } from '@/lib/quickReplies'
import { useImageAttachments } from './useImageAttachments'
import { useSessionDraft, type DraftAttachment } from './useSessionDraft'
import { AttachmentChips } from './AttachmentChips'
import { CommandPalette, type CommandPaletteHandle } from './CommandPalette'
import { MicButton } from './MicButton'
import { useDictationField } from '@/lib/useDictationField'
import type { Command, QueuedItem } from '@/session/events'
import { statusTier } from '@/session/statusTier'
import { caretLineEdges } from '@/lib/caretLineEdges'

interface Props {
  status: { state: string; count: number }
  onPrompt: (text: string, attachmentIds: string[]) => void
  onInterrupt: () => void
  queued?: QueuedItem[]
  onCancelQueued?: (index: number) => void
  chatArmed?: boolean
  commands?: Command[]
  onCommand?: (name: string, args: string) => void
  /** Auto-focus the textarea when this changes / on mount (mirrors DispatchBar). */
  autoFocusKey?: unknown
  suggestions?: string[]
  history?: string[]
  sessionId?: string
}

export function Composer({ status, onPrompt, onInterrupt, queued, onCancelQueued, chatArmed, commands = [], onCommand, autoFocusKey, suggestions, history = [], sessionId }: Props) {
  const draft = useSessionDraft(sessionId)
  const [text, setText] = useState(draft.initial.text)
  const [menuOpen, setMenuOpen] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const paletteRef = useRef<CommandPaletteHandle>(null)
  const isWorking = statusTier(status.state) === 'busy'
  const att = useImageAttachments(
    draft.initial.attachments.map((a, i) => ({
      key: `draft-${i}`, id: a.id, mime: a.mime, name: a.name,
      previewUrl: `/api/attachments/${a.id}`,
    })),
  )

  // Persist the unsent draft so it survives navigation (the Composer remounts
  // per session via the session-keyed runtime provider).
  useEffect(() => {
    const attachments: DraftAttachment[] = att.attachments
      .filter((a) => !a.error && a.id)
      .map((a) => ({ id: a.id as string, mime: a.mime, name: a.name }))
    draft.save({ text, attachments })
  }, [text, att.attachments, draft])
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
      setText(''); draft.clear(); if (textareaRef.current) textareaRef.current.style.height = 'auto'
      histPosRef.current = 0; draftRef.current = ''; return
    }
    onPrompt(trimmed, refs)
    setText(''); att.clear(); draft.clear()
    histPosRef.current = 0; draftRef.current = ''
    // Reset height
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
    }
  }, [text, onPrompt, att, runCommandFromText, draft])

  const prefill = useCallback((value: string) => {
    setText(value)
    const el = textareaRef.current
    if (el) {
      el.focus()
      el.style.height = 'auto'
      el.style.height = `${Math.min(el.scrollHeight, 192)}px`
    }
  }, [])

  const showResume = !isWorking && text === ''

  const play = useCallback(() => {
    // Existing suggestion (shown as ghost text) → send it immediately. Otherwise
    // prefill "continue", focused + editable, for the user to confirm — instant and
    // reliable on any session (a detached/idle session has no live driver to
    // generate against, so on-demand generation can't work there).
    if (suggestions && suggestions.length > 0) {
      onPrompt(suggestions[0], [])
      return
    }
    prefill('continue')
  }, [suggestions, onPrompt, prefill])

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
              key={`${index}-${item.text.slice(0, 20)}`}
              className={cn(
                'flex items-center justify-between gap-2 px-2 py-1 rounded',
                'bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400 text-xs',
              )}
            >
              <div className="flex items-center gap-1.5 flex-1 min-w-0">
                {item.images.map((img, i) => (
                  <img
                    key={i}
                    src={img.url}
                    alt={img.name || 'attachment'}
                    className="h-8 w-8 rounded object-cover flex-shrink-0"
                  />
                ))}
                <span className="truncate flex-1">
                  {item.text.length > 80 ? item.text.slice(0, 80) + '…' : item.text}
                </span>
              </div>
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
              // `block` removes the default inline-block baseline gap: as an inline element the
              // textarea sits on its wrapper's text baseline, leaving ~6px of line-box descent
              // below it, so `items-end` on the row aligned the wrapper (not the textarea) and the
              // box rode high above the send/mic buttons.
              'block w-full resize-none rounded-md border border-gray-300 dark:border-gray-600',
              'bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100',
              'px-3 py-2 text-sm',
              'placeholder:text-gray-400 dark:placeholder:text-gray-500',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
              'min-h-[44px] overflow-y-auto',
            )}
            style={{ maxHeight: '192px' }}
          />
          {ghost && (
            // Tap-to-accept: soft keyboards have no Tab/→, so the overlay itself is the
            // touch affordance. Keyboard users still accept via Tab/→ in handleKeyDown
            // (which fires while the textarea keeps focus); tabIndex -1 keeps this button
            // out of the tab order so it doesn't add a second stop or steal that flow.
            <button
              type="button"
              data-testid="ghost-suggestion"
              tabIndex={-1}
              aria-label={`Use suggestion: ${ghost}`}
              onClick={() => prefill(ghost)}
              className={cn(
                'absolute inset-0 flex items-center justify-between gap-2 text-left',
                'px-3 py-2 text-sm focus-visible:outline-none',
              )}
            >
              <span className="truncate text-gray-400 dark:text-gray-500">{ghost}</span>
              <span className={cn(
                'flex-shrink-0 rounded border px-1 text-[10px] leading-tight',
                'border-gray-300 dark:border-gray-600 text-gray-400 dark:text-gray-500',
              )}>
                ⇥ Tab
              </span>
            </button>
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
          accept="image/*"
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

        {showResume && (
          <div className="relative flex items-center">
            <button
              type="button"
              aria-label="Resume (send suggested next step)"
              title={suggestions && suggestions.length > 0 ? 'Resume — send the suggested next step' : 'Resume — prefill “continue”'}
              onClick={play}
              className={cn(
                'flex-shrink-0 rounded-md px-3 py-2 text-sm font-medium min-h-[44px]',
                'bg-emerald-600 text-white hover:bg-emerald-700',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400',
              )}
            >
              <Sparkles size={16} aria-hidden="true" />
            </button>
            <button
              type="button"
              aria-label="Quick replies"
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
            {menuOpen && (
              <div
                role="menu"
                className={cn(
                  'absolute bottom-full right-0 mb-1 min-w-[12rem] max-w-xs z-10',
                  'rounded-md border border-gray-200 dark:border-gray-700',
                  'bg-white dark:bg-gray-800 shadow-lg py-1',
                )}
              >
                {/* Dynamic section: backend context suggestions the sparkle didn't
                    already send (slice(1)). Empty for most sessions. */}
                {(suggestions ?? []).slice(1).map((s, i) => (
                  <button
                    key={`suggestion-${i}`}
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
                {(suggestions?.length ?? 0) > 1 && (
                  <div role="separator" className="my-1 border-t border-gray-200 dark:border-gray-700" />
                )}
                {/* Canned section: hard-coded one-tap replies, always available. */}
                {QUICK_REPLIES.map((reply) => (
                  <button
                    key={reply}
                    type="button"
                    role="menuitem"
                    onClick={() => { setMenuOpen(false); onPrompt(reply, []) }}
                    className={cn(
                      'block w-full truncate px-3 py-2 text-left text-sm',
                      'hover:bg-gray-100 dark:hover:bg-gray-700',
                    )}
                  >
                    {reply}
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
