import { useEffect, useRef, useState } from 'react'
import { cn } from '@/lib/utils'
import type { GithubRepo } from '@/api/hooks'
import { getLastRepo, setLastRepo } from '@/lib/lastRepo'

/**
 * Returns whether the caret is on the first and/or last line of a textarea value.
 * Used for boundary-aware ↑/↓ navigation: we only navigate the session list when
 * moving the cursor would be a no-op (already at the line edge).
 */
export function caretLineEdges(
  value: string,
  caret: number,
): { atFirstLine: boolean; atLastLine: boolean } {
  const firstNewline = value.indexOf('\n')
  const atFirstLine = firstNewline === -1 || caret <= firstNewline
  const lastNewline = value.lastIndexOf('\n')
  const atLastLine = lastNewline === -1 || caret > lastNewline
  return { atFirstLine, atLastLine }
}

export interface DispatchBarProps {
  repos: GithubRepo[]
  reposLoading: boolean
  reposError: boolean
  /** Fired on Enter when the prompt has text. */
  onDispatch: (input: { initialPrompt: string; repo: string | null }) => void
  /** ↑/↓ at a line boundary (or empty prompt). */
  onNavigate: (dir: 'up' | 'down') => void
  /** Enter on an EMPTY prompt, or →. */
  onOpenHighlighted: () => void
  /** r / F2 / Delete / Alt+1..9 on the highlighted row. */
  onRowAction: (
    action: { type: 'rename' } | { type: 'delete' } | { type: 'jump'; index: number },
  ) => void
  /** ? — open the shortcuts overlay. */
  onShowHelp: () => void
  /** Esc — also clears highlight in the parent. */
  onEscape: () => void
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
  activeDescendantId,
  autoFocusKey,
  isPending,
}: DispatchBarProps) {
  const [text, setText] = useState('')
  const [repo, setRepo] = useState<string>(() => getLastRepo() ?? '')
  const textareaRef = useRef<HTMLTextAreaElement>(null)

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

  function dispatchNow() {
    const trimmed = text.trim()
    if (!trimmed) {
      onOpenHighlighted()
      return
    }
    onDispatch({ initialPrompt: trimmed, repo: repo || null })
    setText('')
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

    // ? (Shift+/) → open help overlay
    // Handle both e.key==='?' (real browser) and e.key==='/' with shiftKey (jsdom/userEvent)
    if (e.key === '?' || (e.key === '/' && e.shiftKey)) {
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

    // ArrowRight at end of text → open highlighted session
    if (e.key === 'ArrowRight' && caret >= text.length) {
      e.preventDefault()
      onOpenHighlighted()
      return
    }

    // r or F2 when prompt is empty → rename highlighted row
    // preventDefault prevents the character from being inserted into the textarea.
    // In tests, user.keyboard('r') respects preventDefault; user.type('fix r') won't
    // trigger this branch because `empty` is false when text already has content.
    if (empty && (e.key === 'r' || e.key === 'F2')) {
      e.preventDefault()
      onRowAction({ type: 'rename' })
      return
    }

    // Delete when prompt is empty → delete (close) highlighted row
    if (empty && e.key === 'Delete') {
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

  return (
    <div
      className={cn(
        'flex flex-col gap-2 px-3 py-2',
        'border-t border-gray-200 dark:border-gray-700',
        'bg-white dark:bg-gray-900',
      )}
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

      {/* Prompt textarea */}
      <textarea
        ref={textareaRef}
        id="dispatch-bar-prompt"
        aria-label="New session prompt"
        aria-controls="session-listbox"
        aria-activedescendant={activeDescendantId}
        value={text}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        rows={1}
        placeholder="New session… (Enter to create, ↑↓ to navigate, ? for shortcuts)"
        disabled={isPending}
        className={cn(
          'w-full resize-none rounded-md border border-gray-300 dark:border-gray-600',
          'bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100',
          'px-3 py-2 text-sm',
          'placeholder:text-gray-400 dark:placeholder:text-gray-500',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
          'min-h-[44px] overflow-y-auto',
          'disabled:opacity-50 disabled:cursor-not-allowed',
        )}
        style={{ maxHeight: '192px' }}
      />
    </div>
  )
}
