import { useEffect, useRef, useState } from 'react'
import { cn } from '@/lib/utils'
import type { GithubRepo } from '@/api/hooks'
import { getLastRepo, setLastRepo } from '@/lib/lastRepo'
import { caretLineEdges } from '@/lib/caretLineEdges'
import { useImageAttachments } from './session/useImageAttachments'
import { AttachmentChips } from './session/AttachmentChips'
import { MicButton } from './session/MicButton'
import { useDictationField } from '@/lib/useDictationField'

export interface DispatchBarProps {
  repos: GithubRepo[]
  reposLoading: boolean
  reposError: boolean
  /** Fired on Enter when the prompt has text or images are attached. */
  onDispatch: (input: { initialPrompt: string; repo: string | null; attachmentIds: string[] }) => void
  /** ↑/↓ at a line boundary (or empty prompt). */
  onNavigate: (dir: 'up' | 'down') => void
  /** Enter on an EMPTY prompt, or →. */
  onOpenHighlighted: () => void
  /** Ctrl+R / Ctrl+X / Alt+1..9 on the highlighted row. */
  onRowAction: (
    action: { type: 'rename' } | { type: 'delete' } | { type: 'jump'; index: number },
  ) => void
  /** ? — open the shortcuts overlay. */
  onShowHelp: () => void
  /** Esc — also clears highlight in the parent. */
  onEscape: () => void
  /** When set, show a "press Ctrl+X again to close '<name>'" hint. */
  confirmDeleteName?: string | null
  /** id of the highlighted row's button, e.g. "row-<id>", for aria-activedescendant. */
  activeDescendantId?: string
  /** Auto-focus the textarea when this changes / on mount. */
  autoFocusKey?: unknown
  isPending: boolean
}

export function DispatchBar({
  repos,
  reposLoading,
  reposError,
  onDispatch,
  onNavigate,
  onOpenHighlighted,
  onRowAction,
  onShowHelp,
  onEscape,
  confirmDeleteName,
  activeDescendantId,
  autoFocusKey,
  isPending,
}: DispatchBarProps) {
  const [text, setText] = useState('')
  const [repo, setRepo] = useState<string>(() => getLastRepo() ?? '')
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const att = useImageAttachments()
  const voice = useDictationField({ value: text, setValue: setText, textareaRef })

  // Auto-focus when autoFocusKey changes (or on mount)
  useEffect(() => {
    textareaRef.current?.focus()
  }, [autoFocusKey])

  // Auto-grow textarea: reset height then set to scrollHeight (capped at ~192px / 8 rows)
  function handleChange(e: React.ChangeEvent<HTMLTextAreaElement>) {
    setText(e.target.value)
    const el = e.target
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 192)}px`
  }

  function handlePaste(e: React.ClipboardEvent) {
    const files = Array.from(e.clipboardData?.files || [])
    if (files.length) { e.preventDefault(); void att.addFiles(files) }
  }

  function handleDrop(e: React.DragEvent) {
    const files = Array.from(e.dataTransfer?.files || [])
    if (files.length) { e.preventDefault(); void att.addFiles(files) }
  }

  function dispatchNow() {
    const trimmed = text.trim()
    const refs = att.refs()
    if (!trimmed && refs.length === 0) {
      onOpenHighlighted()
      return
    }
    onDispatch({ initialPrompt: trimmed, repo: repo || null, attachmentIds: refs })
    setText('')
    att.clear()
    // Reset textarea height
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
    }
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    const el = e.currentTarget
    const caret = el.selectionStart ?? 0
    const empty = text === ''

    // Alt+Digit1..9 → jump to session by index
    if (e.altKey && /^[1-9]$/.test(e.key)) {
      e.preventDefault()
      onRowAction({ type: 'jump', index: Number(e.key) - 1 })
      return
    }

    // Enter (no Shift) → dispatch or open highlighted
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      dispatchNow()
      return
    }

    // Escape → clear text, notify parent
    if (e.key === 'Escape') {
      e.preventDefault()
      setText('')
      onEscape()
      return
    }

    // ? (Shift+/) → open help overlay, ONLY when the prompt is empty — otherwise a
    // literal '?' (very common in prompts, e.g. "why is the sky blue?") must type normally.
    // Handle both e.key==='?' (real browser) and e.key==='/' with shiftKey (jsdom/userEvent)
    if (empty && (e.key === '?' || (e.key === '/' && e.shiftKey))) {
      e.preventDefault()
      onShowHelp()
      return
    }

    // ArrowUp → navigate up only at the first line (or empty)
    if (e.key === 'ArrowUp' && (empty || caretLineEdges(text, caret).atFirstLine)) {
      e.preventDefault()
      onNavigate('up')
      return
    }

    // ArrowDown → navigate down only at the last line (or empty)
    if (e.key === 'ArrowDown' && (empty || caretLineEdges(text, caret).atLastLine)) {
      e.preventDefault()
      onNavigate('down')
      return
    }

    // ArrowRight → open highlighted session, ONLY when the prompt is empty, so it never
    // hijacks cursor movement / loses a draft while you're typing.
    if (empty && e.key === 'ArrowRight') {
      e.preventDefault()
      onOpenHighlighted()
      return
    }

    // Ctrl+R → rename highlighted row. preventDefault ALWAYS so the browser never
    // reloads and discards an in-progress prompt; act only when the prompt is empty.
    if (e.ctrlKey && (e.key === 'r' || e.key === 'R')) {
      e.preventDefault()
      if (empty) onRowAction({ type: 'rename' })
      return
    }

    // Ctrl+X → close (delete) highlighted row; two-press guard lives in the parent.
    // Only when the prompt is empty, so native "cut" still works while editing a draft.
    if (empty && e.ctrlKey && (e.key === 'x' || e.key === 'X')) {
      e.preventDefault()
      onRowAction({ type: 'delete' })
      return
    }
  }

  function handleRepoChange(e: React.ChangeEvent<HTMLSelectElement>) {
    const v = e.target.value
    setRepo(v)
    if (v) setLastRepo(v)
  }

  // Determine if the persisted repo is in the current repo list
  const persistedRepo = repo
  const repoIsKnown = repos.some((r) => r.nameWithOwner === persistedRepo)

  // The Create button is a click affordance for the same action as pressing Enter.
  // Unlike Enter (which falls back to opening the highlighted row when empty), the
  // button is enabled ONLY when there is something to dispatch — a labelled "Create"
  // control should never silently open an existing session.
  const canSubmit = !isPending && (text.trim().length > 0 || att.refs().length > 0)

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
      {/* Repo selector row */}
      <div className="flex items-center gap-2">
        <label
          htmlFor="dispatch-bar-repo"
          className="text-xs text-gray-500 dark:text-gray-400 shrink-0"
        >
          Repository
        </label>
        <select
          id="dispatch-bar-repo"
          aria-label="Repository"
          value={repo}
          onChange={handleRepoChange}
          disabled={reposLoading || reposError}
          className={cn(
            'flex-1 rounded border border-gray-300 dark:border-gray-600 text-sm',
            'bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100',
            'px-2 py-1 min-h-[44px]',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
            'disabled:opacity-50 disabled:cursor-not-allowed',
          )}
        >
          {reposLoading ? (
            <>
              {/* Keep persisted repo value selectable while loading so toHaveValue works */}
              {persistedRepo && <option value={persistedRepo}>{persistedRepo}</option>}
              {!persistedRepo && <option value="">Loading…</option>}
            </>
          ) : reposError ? (
            <option value="">— could not load repos —</option>
          ) : (
            <>
              <option value="">— no repo —</option>
              {/* If persisted repo isn't in the list (e.g. after a repo rename), keep it as an option */}
              {persistedRepo && !repoIsKnown && (
                <option value={persistedRepo}>{persistedRepo}</option>
              )}
              {repos.map((r) => (
                <option key={r.nameWithOwner} value={r.nameWithOwner}>
                  {r.nameWithOwner}
                </option>
              ))}
            </>
          )}
        </select>
      </div>

      {/* Press-twice delete confirmation hint */}
      {confirmDeleteName && (
        <p
          role="status"
          aria-live="polite"
          className="text-xs text-amber-700 dark:text-amber-400 px-1"
        >
          Press Ctrl+X again to close &lsquo;{confirmDeleteName}&rsquo;
        </p>
      )}

      <AttachmentChips attachments={att.attachments} onRemove={att.remove} />

      {/* Prompt textarea + attach button row */}
      <div className="flex items-end gap-2">
        <textarea
          ref={textareaRef}
          id="dispatch-bar-prompt"
          aria-label="New session prompt"
          aria-controls="session-listbox"
          aria-activedescendant={activeDescendantId}
          value={text}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          rows={1}
          placeholder="New session… (Enter to create, ↑↓ to navigate, ? for shortcuts)"
          disabled={isPending}
          className={cn(
            'flex-1 resize-none rounded-md border border-gray-300 dark:border-gray-600',
            'bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100',
            'px-3 py-2 text-sm',
            'placeholder:text-gray-400 dark:placeholder:text-gray-500',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
            'min-h-[44px] overflow-y-auto',
            'disabled:opacity-50 disabled:cursor-not-allowed',
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
          <span aria-hidden="true">📎</span>
        </button>
        <button
          type="button"
          aria-label="Create session"
          onClick={dispatchNow}
          disabled={!canSubmit}
          className={cn(
            'flex-shrink-0 rounded-md px-4 py-2 text-sm font-medium min-h-[44px]',
            'bg-blue-600 text-white',
            'hover:bg-blue-700',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
            'disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:bg-blue-600',
          )}
        >
          Create
        </button>
      </div>
    </div>
  )
}
