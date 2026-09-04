import { useEffect, useRef, useState } from 'react'
import { Sparkles } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { GithubRepo } from '@/api/hooks'
import { useAssessDispatch } from '@/api/hooks'
import type { Account, ModelOption } from '@/api/types'
import { getLastRepo, setLastRepo } from '@/lib/lastRepo'
import { caretLineEdges } from '@/lib/caretLineEdges'
import { isGatedTier, modelName } from '@/session/modelName'
import { useImageAttachments } from './session/useImageAttachments'
import { AttachmentChips } from './session/AttachmentChips'
import { MicButton } from './session/MicButton'
import { useDictationField } from '@/lib/useDictationField'
import { DispatchError } from './dispatchError'

export interface DispatchBarProps {
  repos: GithubRepo[]
  reposLoading: boolean
  reposError: boolean
  /** Accounts offered in the Account dropdown; empty ⇒ only "Auto-select". */
  accounts?: Account[]
  /** Curated model list (server source of truth); empty ⇒ only "Default". */
  models?: ModelOption[]
  /** Fleet hosts for the Runner dropdown; absent or empty ⇒ no dropdown shown. */
  hosts?: { name: string }[]
  /**
   * Fired on Enter when the prompt has text or images are attached.
   * `account`/`model`/`host` are already normalized: null means "let the backend
   * decide" (Auto-select account / account-default model / auto-rank runner).
   * Reject with a `DispatchError` to have the bar show its message inline.
   */
  onDispatch: (input: {
    initialPrompt: string
    repo: string | null
    account: string | null
    model: string | null
    host: string | null
    attachmentIds: string[]
  }) => Promise<void>
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
  hosts = [],
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
  // '' = Auto (let FleetOrchestrator rank); a name = prefer that host.
  const [host, setHost] = useState('')
  // Mobile-only disclosure for the Account/Model selects; desktop always shows all three.
  const [showMore, setShowMore] = useState(false)
  const [assessing, setAssessing] = useState(false)
  const [refinedPrompt, setRefinedPrompt] = useState<string | null>(null)
  const [dispatchError, setDispatchError] = useState<string | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const att = useImageAttachments()
  const voice = useDictationField({ value: text, setValue: setText, textareaRef })
  const { assess } = useAssessDispatch()

  // Auto-focus when autoFocusKey changes (or on mount)
  useEffect(() => {
    textareaRef.current?.focus()
  }, [autoFocusKey])

  // Auto-grow textarea: reset height then set to scrollHeight (capped at ~192px / 8 rows)
  function handleChange(e: React.ChangeEvent<HTMLTextAreaElement>) {
    setText(e.target.value)
    // Editing the draft is the user moving on from the last failure.
    if (dispatchError) setDispatchError(null)
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

  async function handleAssess() {
    setAssessing(true)
    try {
      const result = await assess(text.trim())
      if (result) {
        // Never auto-select a gated tier from assess — leave it for the user to pick explicitly.
        if (result.model && model === 'default' && !isGatedTier(result.model)) setModel(result.model)
        setRefinedPrompt(result.refined_prompt)
      }
    } catch {
      // fail-open: silently ignore errors
    } finally {
      setAssessing(false)
    }
  }

  async function dispatchNow() {
    const trimmed = text.trim()
    const refs = att.refs()
    if (!trimmed && refs.length === 0) {
      onOpenHighlighted()
      return
    }
    setDispatchError(null)
    try {
      await onDispatch({
        initialPrompt: trimmed,
        repo: repo || null,
        account: account || null,
        // "default" ⇒ omit so the backend leaves the account's model as-is.
        model: model && model !== 'default' ? model : null,
        // '' ⇒ null so the backend auto-ranks; a name pins the runner.
        host: host || null,
        attachmentIds: refs,
      })
      setText('')
      setRefinedPrompt(null)
      att.clear()
      if (textareaRef.current) {
        textareaRef.current.style.height = 'auto'
      }
    } catch (e) {
      // Creation failed — leave the text so the user can retry or edit. Say
      // why only when the caller had something to say (see `DispatchError`).
      if (e instanceof DispatchError) setDispatchError(e.message)
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

  // Account/Model/Host are worth flagging when collapsed on mobile — a hidden
  // non-default pick would otherwise silently steer the dispatch.
  const hasNonDefaultPick = account !== '' || model !== 'default' || host !== ''

  return (
    <div
      className={cn(
        'flex flex-col gap-2 px-3 pt-2',
        'border-t border-gray-200 dark:border-gray-700',
        'bg-white dark:bg-gray-900',
      )}
      // Clear the mobile gesture/home bar, same as Composer. env() is 0 on desktop.
      style={{ paddingBottom: 'calc(env(safe-area-inset-bottom) + 0.5rem)' }}
      onDrop={handleDrop}
      onDragOver={(e) => e.preventDefault()}
    >
      {/* Repo / account / model / runner selector row — 3–4 across on desktop; on mobile a
          single line (repo + "more" toggle) with account/model/runner behind the toggle */}
      <div className={cn('grid grid-cols-1 gap-2', hosts.length > 0 ? 'sm:grid-cols-4' : 'sm:grid-cols-3')}>
        {/* Repository + mobile-only more-options toggle. Top-aligned (not items-end): grid
            cells stretch to the tallest column, and the Model column grows when its approval
            hint shows — items-end would drop this label+select to the bottom of the row. On
            mobile the label is hidden and select + toggle are both 44px, so they stay level. */}
        <div className="flex items-start gap-2">
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
            aria-controls="dispatch-bar-account dispatch-bar-model dispatch-bar-runner"
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
                <span className="sr-only">Non-default account, model, or runner selected</span>
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
            {accounts.filter((a) => a.enabled !== false).map((a) => (
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
          {isGatedTier(model) && (
            <p className="text-xs text-amber-600 dark:text-amber-400">
              {modelName(model) || model} requires approval — this session will be held until approved.
            </p>
          )}
        </div>

        {/* Runner — '' = Auto (fleet auto-ranks); a name pins the session to that host. */}
        {hosts.length > 0 && (
          <div className={cn('flex-col gap-1', showMore ? 'flex' : 'hidden sm:flex')}>
            <label htmlFor="dispatch-bar-runner" className={selectLabelClass}>
              Runner
            </label>
            <select
              id="dispatch-bar-runner"
              aria-label="Runner"
              value={host}
              onChange={(e) => setHost(e.target.value)}
              className={selectClass}
            >
              <option value="">Auto</option>
              {hosts.map((h) => (
                <option key={h.name} value={h.name}>{h.name}</option>
              ))}
            </select>
          </div>
        )}
      </div>

      <AttachmentChips attachments={att.attachments} onRemove={att.remove} />

      {/* The last failed dispatch, beside the draft it kept — not a toast over
          the page header. Cleared on the next edit or attempt. */}
      {dispatchError && (
        <p role="alert" className="text-sm text-danger">{dispatchError}</p>
      )}

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

        {text.trim().length > 0 && !isPending && (
          <button
            type="button"
            aria-label="Assess and refine prompt"
            onClick={handleAssess}
            disabled={assessing}
            className={cn(
              'flex-shrink-0 rounded-md px-3 py-2 text-sm min-h-[44px]',
              'border border-gray-300 dark:border-gray-600',
              'text-gray-600 dark:text-gray-300',
              'hover:bg-gray-100 dark:hover:bg-gray-800',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
              'disabled:opacity-50 disabled:cursor-not-allowed',
            )}
          >
            <Sparkles className="h-4 w-4" aria-hidden="true" />
          </button>
        )}

        <MicButton
          supported={voice.supported}
          listening={voice.listening}
          error={voice.error}
          onToggle={voice.toggle}
        />

        {/* Programmatic picker only — the labelled control is the "Attach image"
            button beside it. Kept out of the a11y tree (and out of the tab
            order, which `aria-hidden` on a focusable element would otherwise
            break) so it is not announced as an unlabelled form field: `hidden`
            is a class, and axe under jsdom sees no stylesheet. */}
        <input
          ref={fileRef}
          type="file"
          accept="image/*"
          multiple
          tabIndex={-1}
          aria-hidden="true"
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

      {refinedPrompt !== null && (
        <div className={cn(
          'flex items-start gap-2 rounded-md px-3 py-2 text-sm',
          'bg-blue-50 dark:bg-blue-950 border border-blue-200 dark:border-blue-800',
          'text-gray-800 dark:text-gray-200',
        )}>
          <span className="flex-1 min-w-0 break-words">
            <span aria-hidden="true">✨</span>{' '}
            <span className="font-medium">Refined:</span>{' '}
            {refinedPrompt}
          </span>
          <div className="flex flex-shrink-0 gap-1">
            <button
              type="button"
              onClick={() => {
                setText(refinedPrompt)
                setRefinedPrompt(null)
                if (textareaRef.current) {
                  textareaRef.current.style.height = 'auto'
                }
              }}
              className={cn(
                'rounded px-2 py-1 text-xs font-medium',
                'bg-blue-600 text-white hover:bg-blue-700',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
              )}
            >
              Accept
            </button>
            <button
              type="button"
              onClick={() => setRefinedPrompt(null)}
              className={cn(
                'rounded px-2 py-1 text-xs font-medium',
                'border border-gray-300 dark:border-gray-600',
                'text-gray-600 dark:text-gray-300',
                'hover:bg-gray-100 dark:hover:bg-gray-800',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
              )}
            >
              Discard
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
