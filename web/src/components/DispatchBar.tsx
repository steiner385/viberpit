import { useEffect, useMemo, useRef, useState } from 'react'
import { Sparkles } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { GithubRepo } from '@/api/hooks'
import { useAssessDispatch } from '@/api/hooks'
import type { AgentEngine, Account, EngineOffer, ModelOption } from '@/api/types'
import {
  AGENT_ENGINE_LABELS, DEFAULT_AGENT_ENGINE, accountEngine,
} from '@/api/types'
import { engineReasonLabel, hostDispatchable, type HostEngineState } from '@/api/fleet'
import { getLastRepo, setLastRepo } from '@/lib/lastRepo'
import { getPreferredEngine, setPreferredEngine } from '@/lib/enginePreference'
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
  /** Accounts offered in the Account dropdown; empty ⇒ only "Auto-select".
   *  Filtered here by the selected engine — an account belongs to exactly one. */
  accounts?: Account[]
  /** Model list for the CURRENT engine/account (the dock fetches it; see
   *  `onEngineContextChange`); empty ⇒ only "Default". */
  models?: ModelOption[]
  /** Fleet hosts for the Runner dropdown; absent, empty, or none able to run the
   *  selected engine ⇒ no dropdown shown. */
  hosts?: { name: string; engines?: HostEngineState[] }[]
  /**
   * What engines this deployment offers, from `GET /api/config`. Fewer than two
   * ⇒ no Agent selector at all, which is what every pre-Codex edge reports.
   */
  engines?: EngineOffer[]
  /**
   * Fired on Enter when the prompt has text or images are attached.
   * `account`/`model`/`host`/`reasoningEffort` are already normalized: null means
   * "let the backend decide" (Auto-select account / account-default model /
   * auto-rank runner / model-default effort). `engine` is always concrete.
   * Reject with a `DispatchError` to have the bar show its message inline.
   */
  onDispatch: (input: {
    initialPrompt: string
    repo: string | null
    account: string | null
    model: string | null
    host: string | null
    attachmentIds: string[]
    engine: AgentEngine
    reasoningEffort: string | null
  }) => Promise<void>
  /**
   * The engine/account the model list must be fetched for.
   *
   * The bar owns those two picks, but `GET /api/models` is engine- AND
   * account-scoped for Codex, so the dock cannot know what to fetch without
   * being told. Reported upward rather than queried here so the bar stays a
   * presentational component with no data hooks of its own.
   */
  onEngineContextChange?: (context: { engine: AgentEngine; account: string | null }) => void
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
  /**
   * Whether a session list is on screen for this bar to drive. Default true.
   *
   * The bar has always assumed one: it points `aria-controls` at
   * `#session-listbox`, names a `row-<id>` in `aria-activedescendant`, and
   * swallows Tab / ↑↓ / ←→ / Alt+digit to move that list's cursor. The Sessions
   * column has TWO scopes now, and the archived one renders
   * `ArchivedScopeBody` — so `SessionList`, which owns that id and every
   * `row-<id>`, is not mounted while the dock still is.
   *
   * With this false the bar stays a working composer and stops pretending:
   * both ARIA references are dropped (they were dangling — axe
   * `aria-valid-attr-value`), and every list key falls through to its native
   * behaviour. Tab in particular MUST be gated here rather than by handing the
   * bar an inert `onEnterList`: the `preventDefault` is this component's, so a
   * no-op callback would leave focus pinned to the prompt — a keyboard trap.
   */
  drivesList?: boolean
}

export function DispatchBar({
  repos,
  reposLoading,
  reposError,
  accounts = [],
  models = [],
  hosts = [],
  engines = [],
  onDispatch,
  onEngineContextChange,
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
  drivesList = true,
}: DispatchBarProps) {
  const [text, setText] = useState('')
  const [repo, setRepo] = useState<string>(() => getLastRepo() ?? '')
  // '' = Auto-select (let the backend pick). 'default' = the account's own model.
  const [account, setAccount] = useState('')
  const [model, setModel] = useState('default')
  // '' = Auto (let FleetOrchestrator rank); a name = prefer that host.
  const [host, setHost] = useState('')
  // '' = the model's own default effort; a value pins it.
  const [reasoning, setReasoning] = useState('')
  // null until the user has explicitly picked an agent. NOT seeded to
  // 'claude_code': the resolution below must be able to tell "never chose" from
  // "chose Claude", and an unoffered persisted choice must fall back rather than
  // be honoured.
  const [engineChoice, setEngineChoice] = useState<AgentEngine | null>(() => getPreferredEngine())
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

  // ── which agent is selected ────────────────────────────────────────────────
  //
  // Derived, not stored, so a persisted choice the server has since withdrawn
  // (Codex uninstalled, credential authority removed) can never be honoured —
  // an `available: false` engine falls back to Claude Code every render rather
  // than only on the mount that happened to notice.
  const engine: AgentEngine =
    engineChoice && engines.some((e) => e.engine === engineChoice && e.available)
      ? engineChoice
      : DEFAULT_AGENT_ENGINE
  const engineOffer = engines.find((e) => e.engine === engine)
  const capabilities = engineOffer?.capabilities

  // Changing the agent invalidates every downstream pick: a Claude account
  // cannot run Codex, and the model/effort catalogues do not overlap at all.
  // Reset to the visible sentinels ('' / 'default'), never merely filter them
  // out of the list — a hidden-but-still-set account would be sent to the
  // backend for the wrong engine.
  const lastEngineRef = useRef(engine)
  useEffect(() => {
    if (lastEngineRef.current === engine) return
    lastEngineRef.current = engine
    setAccount('')
    setModel('default')
    setReasoning('')
    setHost('')
  }, [engine])

  // The dock fetches the model list, and for Codex it needs BOTH of these.
  useEffect(() => {
    onEngineContextChange?.({ engine, account: account || null })
  }, [engine, account, onEngineContextChange])

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

  /** The one way the model changes — it also re-seeds the reasoning effort to
   *  whatever the newly-selected model advertises as its default. */
  function selectModel(id: string) {
    setModel(id)
    setReasoning(models.find((m) => m.id === id)?.defaultReasoningEffort ?? '')
  }

  async function handleAssess() {
    setAssessing(true)
    try {
      const result = await assess(text.trim())
      if (result) {
        // Never auto-select a gated tier from assess — leave it for the user to pick explicitly.
        if (result.model && model === 'default' && !isGatedTier(result.model)) selectModel(result.model)
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
        engine,
        // '' ⇒ null so the engine keeps the model's own default effort.
        reasoningEffort: reasoning || null,
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

    // Alt+Digit1..9 → jump to session by index. List-cursor behaviour — no list,
    // no jump; let the key fall through.
    if (drivesList && e.altKey && /^[1-9]$/.test(e.key)) {
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

    // ArrowUp → navigate up only at the first line (or empty). List-cursor
    // behaviour — with no list on screen, let the key move the caret/scroll natively.
    if (drivesList && e.key === 'ArrowUp' && (empty || caretLineEdges(text, caret).atFirstLine)) {
      e.preventDefault()
      onNavigate('up')
      return
    }

    // ArrowDown → navigate down only at the last line (or empty)
    if (drivesList && e.key === 'ArrowDown' && (empty || caretLineEdges(text, caret).atLastLine)) {
      e.preventDefault()
      onNavigate('down')
      return
    }

    // ArrowRight (empty prompt) → expand a highlighted header, else open the highlighted session.
    if (drivesList && empty && e.key === 'ArrowRight') {
      e.preventDefault()
      if (activeDescendantIsHeader) onExpandHighlighted?.()
      else onOpenHighlighted()
      return
    }

    // ArrowLeft (empty prompt) → collapse a highlighted header. (No-op otherwise.)
    if (drivesList && empty && e.key === 'ArrowLeft' && activeDescendantIsHeader) {
      e.preventDefault()
      onCollapseHighlighted?.()
      return
    }

    // Tab on an empty prompt → enter list-focus mode. With a draft, Tab keeps its
    // native job (move focus to the Create button) so keyboard send still works.
    // With no list on screen (drivesList false) Tab ALWAYS keeps its native job —
    // gated here, not by handing in an inert `onEnterList`, because the
    // `preventDefault` below is what would otherwise pin focus to the prompt.
    if (drivesList && empty && e.key === 'Tab' && !e.shiftKey) {
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

  // ── what the selected engine leaves on offer ───────────────────────────────

  /** Accounts that can run THIS engine. An account is scoped to exactly one
   *  (constitution II), and an engine-less row is a pre-Codex Claude account. */
  const engineAccounts = useMemo(
    () => accounts.filter((a) => a.enabled !== false && accountEngine(a) === engine),
    [accounts, engine],
  )

  /** Runners this engine may actually be dispatched to. Shrinking the list is
   *  the honest answer: pinning a Codex launch to a host that cannot start it
   *  is a 409 the user could not have predicted. */
  const engineHosts = useMemo(
    () => hosts.filter((h) => hostDispatchable(h, engine)),
    [hosts, engine],
  )

  // Codex model lists come from the selected ACCOUNT's own app-server, so with
  // no account there is nothing to show — and asking anyway is a 400.
  const needsAccountForModels = engine !== DEFAULT_AGENT_ENGINE && !account

  const selectedModel = models.find((m) => m.id === model)
  // Capabilities — not `if (engine === 'codex')` — decide whether the control
  // exists at all; the MODEL then decides whether it has anything to offer.
  // An edge that reports no capabilities (pre-Codex `/api/config`) does not
  // suppress a model that explicitly advertises efforts.
  const reasoningEfforts =
    (capabilities?.reasoningEffort ?? true) ? selectedModel?.reasoningEfforts ?? [] : []
  const showReasoning = reasoningEfforts.length > 0
  const showRunner = engineHosts.length > 0
  // Two known engines is the trigger, not two AVAILABLE ones: an unavailable
  // engine still renders (disabled, with its reason) so "why can't I pick
  // Codex?" is answerable without opening Settings.
  const showEngine = engines.length > 1

  // Everything behind the mobile disclosure is worth flagging when collapsed —
  // a hidden non-default pick would otherwise silently steer the dispatch.
  const hasNonDefaultPick =
    engine !== DEFAULT_AGENT_ENGINE ||
    account !== '' ||
    model !== 'default' ||
    host !== '' ||
    (reasoning !== '' && reasoning !== (selectedModel?.defaultReasoningEffort ?? ''))

  // Built from the ids actually rendered: a dangling `aria-controls` target is
  // an a11y violation, and the Reasoning/Runner selects are conditional.
  const disclosedIds = [
    showEngine && 'dispatch-bar-engine',
    'dispatch-bar-account',
    'dispatch-bar-model',
    showReasoning && 'dispatch-bar-reasoning',
    showRunner && 'dispatch-bar-runner',
  ].filter((id): id is string => typeof id === 'string')

  // One column per rendered select (plus the repo cell). Literal class names so
  // Tailwind's scanner emits them.
  const columnClass = {
    2: 'sm:grid-cols-2',
    3: 'sm:grid-cols-3',
    4: 'sm:grid-cols-4',
    5: 'sm:grid-cols-5',
    6: 'sm:grid-cols-6',
  }[Math.min(6, 1 + disclosedIds.length)] ?? 'sm:grid-cols-4'

  /** Why an engine cannot be picked. An edge-side blocker wins: when the EDGE
   *  is the problem no runner is at fault, and naming one would send the
   *  operator to the wrong machine. */
  function engineUnavailableText(o: EngineOffer): string {
    if (o.blockedReason) return engineReasonLabel(o.blockedReason)
    const names = Object.keys(o.unavailableReasons)
    if (names.length === 0) return 'unavailable'
    return `no runner can run it (${names.map((n) => `${n}: ${engineReasonLabel(o.unavailableReasons[n])}`).join(', ')})`
  }

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
      {/* Repo / agent / account / model / reasoning / runner selector row — one column
          per rendered select on desktop; on mobile a single line (repo + "more" toggle)
          with everything else behind the toggle */}
      <div className={cn('grid grid-cols-1 gap-2', columnClass)}>
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
            aria-controls={disclosedIds.join(' ')}
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
                <span className="sr-only">
                  Non-default agent, account, model, reasoning, or runner selected
                </span>
              </>
            )}
          </button>
        </div>

        {/* Agent — which engine runs the session. Rendered only when the server
            knows about more than one; an engine it cannot currently offer is
            listed but disabled, with the reason, rather than silently missing. */}
        {showEngine && (
          <div className={cn('flex-col gap-1', showMore ? 'flex' : 'hidden sm:flex')}>
            <label htmlFor="dispatch-bar-engine" className={selectLabelClass}>
              Agent
            </label>
            <select
              id="dispatch-bar-engine"
              aria-label="Agent"
              value={engine}
              onChange={(e) => {
                const next = e.target.value as AgentEngine
                setEngineChoice(next)
                setPreferredEngine(next)
              }}
              className={selectClass}
            >
              {engines.map((o) => (
                <option key={o.engine} value={o.engine} disabled={!o.available}>
                  {AGENT_ENGINE_LABELS[o.engine] ?? o.engine}
                  {o.available ? '' : ` — ${engineUnavailableText(o)}`}
                </option>
              ))}
            </select>
          </div>
        )}

        {/* Account — "" = Auto-select (usage-aware rotation picks one). Only the
            accounts that belong to the selected engine. */}
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
            {engineAccounts.map((a) => (
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
            value={needsAccountForModels ? '' : model}
            onChange={(e) => selectModel(e.target.value)}
            disabled={needsAccountForModels}
            className={selectClass}
          >
            {/* This engine's catalogue is a property of the ACCOUNT, so say so
                rather than firing a request the server answers with a 400. */}
            {needsAccountForModels && <option value="">Select an account first</option>}
            {/* Fallback so the "default" value always has a matching option before the list loads. */}
            {!needsAccountForModels && models.length === 0 && <option value="default">Default</option>}
            {!needsAccountForModels && models.map((m) => (
              <option key={m.id} value={m.id}>{m.label}</option>
            ))}
          </select>
          {isGatedTier(model) && (
            <p className="text-xs text-amber-600 dark:text-amber-400">
              {modelName(model) || model} requires approval — this session will be held until approved.
            </p>
          )}
        </div>

        {/* Reasoning effort — only when the engine advertises the capability AND
            the selected model offers choices; seeded to the model's own default. */}
        {showReasoning && (
          <div className={cn('flex-col gap-1', showMore ? 'flex' : 'hidden sm:flex')}>
            <label htmlFor="dispatch-bar-reasoning" className={selectLabelClass}>
              Reasoning
            </label>
            <select
              id="dispatch-bar-reasoning"
              aria-label="Reasoning effort"
              value={reasoning}
              onChange={(e) => setReasoning(e.target.value)}
              className={selectClass}
            >
              {reasoningEfforts.map((effort) => (
                <option key={effort} value={effort}>{effort}</option>
              ))}
            </select>
          </div>
        )}

        {/* Runner — '' = Auto (fleet auto-ranks); a name pins the session to that host. */}
        {showRunner && (
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
              {engineHosts.map((h) => (
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
          aria-controls={drivesList ? 'session-listbox' : undefined}
          aria-activedescendant={drivesList ? activeDescendantId : undefined}
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
