import { useEffect, useRef, useState } from 'react'
import { cn } from '@/lib/utils'
import type { GithubRepo } from '@/api/hooks'
import type { Account, ModelOption } from '@/api/types'
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
  /** Accounts offered in the Account dropdown; empty ⇒ only "Auto-select". */
  accounts?: Account[]
  /** Curated model list (server source of truth); empty ⇒ only "Default". */
  models?: ModelOption[]
  /**
   * Fired on Enter when the prompt has text or images are attached.
   * `account`/`model` are already normalized: null means "let the backend
   * decide" (Auto-select account / account-default model).
   */
  onDispatch: (input: {
    initialPrompt: string
    repo: string | null
    account: string | null
    model: string | null
    attachmentIds: string[]
  }) => void
  /** ↑/↓ at a line boundary (or empty prompt). */
  onNavigate: (dir: 'up' | 'down') => void
  /** Enter on an EMPTY prompt, or →. */
  onOpenHighlighted: () => void
  /** Tab on an empty prompt — drop into list-focus mode. */
  onEnterList: () => void
  /** Alt+1..9 — open session by index. */
  onJump: (index: number) => void
  /** ? — open the shortcuts overlay. */
  onShowHelp: () => void
  /** Esc — also clears highlight in the parent. */
  onEscape: () => void
  /** id of the highlighted row's button, e.g. "row-<id>", for aria-activedescendant. */
  activeDescendantId?: string
  /** Auto-focus the textarea when this changes / on mount. */
  autoFocusKey?: unknown
  isPending: boolean
  /** Empty prompt + header highlighted: → expands it. */
  onExpandHighlighted?: () => void
  /** Empty prompt + header highlighted: ← collapses it. */
  onCollapseHighlighted?: () => void
  /** Empty prompt + header highlighted: Enter toggles it. */
  onToggleHighlighted?: () => void
  /** True when the active descendant is a group header (not a session row). */
  activeDescendantIsHeader?: boolean
}

export function DispatchBar({
  repos,
  reposLoading,
  reposError,
  accounts = [],
  models = [],
  onDispatch,
  onNavigate,
  onOpenHighlighted,
  onEnterList,
  onJump,
  onShowHelp,
  onEscape,
  activeDescendantId,
  autoFocusKey,
  isPending,
  onExpandHighlighted,
  onCollapseHighlighted,
  onToggleHighlighted,
  activeDescendantIsHeader,
}: DispatchBarProps) {
  const [text, setText] = useState('')
  const [repo, setRepo] = useState<string>(() => getLastRepo() ?? '')
  // '' = Auto-select (let the backend pick). 'default' = the account's own model.
  const [account, setAccount] = useState('')
  const [model, setModel] = useState('default')
  // Mobile-only disclosure for the Account/Model selects; desktop always shows all three.
  const [showMore, setShowMore] = useState(false)
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
    onDispatch({
      initialPrompt: trimmed,
      repo: repo || null,
      account: account || null,
      // "default" ⇒ omit so the backend leaves the account's model as-is.
      model: model && model !== 'default' ? model : null,
      attachmentIds: refs,
    })
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
      onJump(Number(e.key) - 1)
      return
    }

    // Enter (no Shift) → toggle a highlighted header (empty prompt), else dispatch/open.
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      if (empty && activeDescendantIsHeader) { onToggleHighlighted?.(); return }
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

    // ArrowRight (empty prompt) → expand a highlighted header, else open the highlighted session.
    if (empty && e.key === 'ArrowRight') {
      e.preventDefault()
      if (activeDescendantIsHeader) onExpandHighlighted?.()
      else onOpenHighlighted()
      return
    }

    // ArrowLeft (empty prompt) → collapse a highlighted header. (No-op otherwise.)
    if (empty && e.key === 'ArrowLeft' && activeDescendantIsHeader) {
      e.preventDefault()
      onCollapseHighlighted?.()
      return
    }

    // Tab on an empty prompt → enter list-focus mode. With a draft, Tab keeps its
    // native job (move focus to the Create button) so keyboard send still works.
    if (empty && e.key === 'Tab' && !e.shiftKey) {
      e.preventDefault()
      onEnterList()
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

  // Shared styling for the repo / account / model selects (single source of truth).
  const selectClass = cn(
    'w-full rounded border border-gray-300 dark:border-gray-600 text-sm',
    'bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100',
    'px-2 py-1 min-h-[44px]',
    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
    'disabled:opacity-50 disabled:cursor-not-allowed',
  )
  const selectLabelClass = 'text-xs text-gray-500 dark:text-gray-400'

  // Account/Model are worth flagging when collapsed on mobile — a hidden
  // non-default pick would otherwise silently steer the dispatch.
  const hasNonDefaultPick = account !== '' || model !== 'default'

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
      {/* Repo / account / model selector row — 3 across on desktop; on mobile a
          single line (repo + "more" toggle) with account/model behind the toggle */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
        {/* Repository + mobile-only more-options toggle */}
        <div className="flex items-end gap-2">
          <div className="flex min-w-0 flex-1 flex-col gap-1">
          <label htmlFor="dispatch-bar-repo" className={cn(selectLabelClass, 'hidden sm:block')}>
            Repository
          </label>
          <select
            id="dispatch-bar-repo"
            aria-label="Repository"
            value={repo}
            onChange={handleRepoChange}
            disabled={reposLoading || reposError}
            className={selectClass}
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
          <button
            type="button"
            aria-label="More options"
            aria-expanded={showMore}
            aria-controls="dispatch-bar-account dispatch-bar-model"
            onClick={() => setShowMore((v) => !v)}
            className={cn(
              'sm:hidden relative flex-shrink-0 rounded-md px-3 py-2 text-sm min-h-[44px]',
              'border border-gray-300 dark:border-gray-600',
              'text-gray-600 dark:text-gray-300',
              'hover:bg-gray-100 dark:hover:bg-gray-800',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
            )}
          >
            <span aria-hidden="true">⋯</span>
            {!showMore && hasNonDefaultPick && (
              <>
                <span
                  aria-hidden="true"
                  className="absolute top-1 right-1 h-2 w-2 rounded-full bg-blue-500"
                />
                <span className="sr-only">Non-default account or model selected</span>
              </>
            )}
          </button>
        </div>

        {/* Account — "" = Auto-select (usage-aware rotation picks one). */}
        <div className={cn('flex-col gap-1', showMore ? 'flex' : 'hidden sm:flex')}>
          <label htmlFor="dispatch-bar-account" className={selectLabelClass}>
            Account
          </label>
          <select
            id="dispatch-bar-account"
            aria-label="Account"
            value={account}
            onChange={(e) => setAccount(e.target.value)}
            className={selectClass}
          >
            <option value="">Auto-select</option>
            {accounts.map((a) => (
              <option key={a.name} value={a.name}>{a.nickname || a.name}</option>
            ))}
          </select>
        </div>

        {/* Model — "default" = the account's own model. */}
        <div className={cn('flex-col gap-1', showMore ? 'flex' : 'hidden sm:flex')}>
          <label htmlFor="dispatch-bar-model" className={selectLabelClass}>
            Model
          </label>
          <select
            id="dispatch-bar-model"
            aria-label="Model"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            className={selectClass}
          >
            {/* Fallback so the "default" value always has a matching option before the list loads. */}
            {models.length === 0 && <option value="default">Default</option>}
            {models.map((m) => (
              <option key={m.id} value={m.id}>{m.label}</option>
            ))}
          </select>
        </div>
      </div>

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
