import { Suspense, lazy, useEffect, useId, useRef, useState } from 'react'
import type { Block } from '@/session/reducer'
import type { RulePreview, RuleScope } from '@/api/types'
import { AlwaysAllowDialog } from './AlwaysAllowDialog'
import { cn } from '@/lib/utils'
import { useCardKeyboard } from '@/features/sessions/useCardKeyboard'
import { toolLabel, toolDetail } from '@/session/toolSummary'
import { composePlanFeedback, PLAN_FEEDBACK_MAX } from '../review/planFeedback'
import type { LineComment } from '../review/reviewComments'

/**
 * The numbered, commentable plan lines (D-5) load only when "Keep planning" opens comment mode:
 * the plan card is first-paint. They ride the Changes tab's existing chunk (which already holds the
 * gutter and editor) rather than a chunk of their own — a new chunk sharing those modules would
 * regroup the shared chunks and grow the initial preload. A failed load (offline, a deploy
 * rollover) falls back to the plain plan text, so the overall comment still sends.
 */
const PlanStepLines = lazy(() => import('../diff/ChangesTab').then(
  (m) => ({ default: m.PlanStepLines }),
  () => ({ default: ({ text }: { text: string }) => <PlainDetail text={text} /> }),
))

type PermBlock = Extract<Block, { kind: 'permission' }>
type Decision = 'allow' | 'deny' | 'always'
/** A card button: 'session' sends the `always` decision; 'chooser' opens the standing-rule dialog;
 *  'keepPlanning' (plan cards) opens the optional comment field rather than answering. */
type CardAction = 'allow' | 'deny' | 'session' | 'chooser' | 'keepPlanning'

interface Props {
  block: PermBlock
  /** `message` only ever rides with `deny` — a plan card's "keep planning" comment. */
  onDecision: (permId: string, decision: Decision, message?: string) => void
  /** Standing-rule wiring. With both rule callbacks present (and a card that allows standing
   *  grants with rule suggestions) an "Always allow…" button opens the chooser, at "This repo" when
   *  `hasRepo`. Without them (sub-agent page, assistant drawer) there is no Always allow… button;
   *  "Allow for this session" is always its own button and sends the `always` decision directly. */
  hasRepo?: boolean
  onPreviewRule?: (permId: string, pattern: string, scope: RuleScope) => Promise<RulePreview>
  onGrantRule?: (permId: string, pattern: string, scope: RuleScope, confirm: boolean) => Promise<unknown>
}

// Weighting is the message (2026-09-29 critique, P0): the ONE-TIME grant is the
// filled primary, the refusal is the outlined secondary, and the STANDING grant
// ("Always allow") is a quiet text-weight tertiary — it used to wear the heaviest
// button on the card, nudging the highest-stakes tap toward the broadest grant.
const primaryBtn = cn(
  'min-h-target rounded px-4 py-2 text-sm font-semibold',
  'bg-accent text-accent-ink hover:bg-accent/90',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
)
const allowBtn = cn(
  'min-h-target rounded px-4 py-2 text-sm font-semibold',
  'bg-success-strong text-on-strong hover:bg-success-strong/90',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
)
const secondaryBtn = cn(
  'min-h-target rounded px-4 py-2 text-sm',
  'bg-surface border border-edge-strong',
  'text-ink hover:bg-surface-raised',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
)
const tertiaryBtn = cn(
  'min-h-target rounded px-3 py-2 text-sm',
  'text-ink-muted underline-offset-4 hover:underline hover:text-ink',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
)

export function PermissionCard({ block, onDecision, hasRepo, onPreviewRule, onGrantRule }: Props) {
  const [chooserOpen, setChooserOpen] = useState(false)
  // Plan cards: Keep planning opens an inline, optional comment instead of answering at once.
  const [commenting, setCommenting] = useState(false)
  const [comment, setComment] = useState('')
  const [steps, setSteps] = useState<LineComment[]>([])
  const commentId = useId()
  const commentRef = useRef<HTMLTextAreaElement | null>(null)
  const keepPlanningRef = useRef<HTMLButtonElement | null>(null)
  useEffect(() => {
    if (commenting) commentRef.current?.focus()
  }, [commenting])
  const label = toolLabel(block.tool, block.toolKind, block.input)
  // The whole request, not the 60-char gist: a command the operator cannot read
  // is a command they approve blind.
  const detail = toolDetail(block.toolKind, block.input)
  // Destructive app-action tools (spec §4.4) never offer "Always allow", in
  // every mode — the server marks this on the request rather than the client
  // inferring it from toolKind, since a MUTATION app_action IS grantable.
  const canAlwaysAllow = block.alwaysAllowed !== false && !block.plan
  // Server-proposed standing rules → the chooser. A grantable card with none (a Codex
  // edit-root approval, or rules off) keeps the direct session-wide accept.
  const hasSuggestions = canAlwaysAllow && (block.ruleSuggestions?.length ?? 0) > 0
  const rules = hasSuggestions && onPreviewRule && onGrantRule && hasRepo !== undefined
    ? { hasRepo, onPreviewRule, onGrantRule }
    : null

  // The server says whether "Allow for this session" would really grant; when it would only
  // degrade to allow-once, the button is hidden rather than promising a grant.
  const canSessionGrant = canAlwaysAllow && block.sessionGrantEffective !== false

  // Ordered actions matching the rendered buttons; digit/arrow indices map here.
  // Plan cards keep their own two-button layout. A grantable permission card is
  // Allow once · Allow for this session · [Always allow… when the chooser is available] · Deny.
  const actions: CardAction[] = block.plan
    ? ['allow', 'keepPlanning']
    : !canAlwaysAllow ? ['allow', 'deny']
    : ['allow', ...(canSessionGrant ? ['session' as const] : []),
       ...(rules ? ['chooser' as const] : []), 'deny']

  const activate = (a: CardAction) => {
    if (a === 'chooser') setChooserOpen(true)
    else if (a === 'keepPlanning') setCommenting(true)
    else onDecision(block.permId, a === 'session' ? 'always' : a)
  }

  // The plan as sent (`input.plan`), else the request's detail text, as commentable lines.
  const planText = typeof block.input.plan === 'string' ? block.input.plan : detail
  // Overall text plus the per-step comments; past the server's deny-message cap Send is disabled.
  const feedback = composePlanFeedback(comment, steps)
  const tooLong = feedback.length > PLAN_FEEDBACK_MAX

  // Blank feedback is "Keep planning" exactly as it was: a bare deny, no message key.
  const sendKeepPlanning = () => {
    if (tooLong) return
    if (feedback) onDecision(block.permId, 'deny', feedback)
    else onDecision(block.permId, 'deny')
  }
  const cancelComment = () => {
    setCommenting(false)
    setComment('')
    setSteps([])
    keepPlanningRef.current?.focus()
  }

  const kb = useCardKeyboard({
    count: actions.length,
    onActivate: (i) => activate(actions[i]),
  })

  // Keep planning needs its own ref too (focus returns to it on Cancel), so its roving props are split.
  const keepItem = kb.itemProps(1)

  // `A` opens the chooser from anywhere inside the card (not from a text field).
  const onCardKeyDown = (e: React.KeyboardEvent) => {
    if (!rules || e.metaKey || e.ctrlKey || e.altKey) return
    const tag = (e.target as HTMLElement).tagName
    if (tag === 'INPUT' || tag === 'TEXTAREA') return
    if (e.key === 'a' || e.key === 'A') {
      e.preventDefault(); e.stopPropagation()
      setChooserOpen(true)
    }
  }

  return (
    <>
    <div
      role="group"
      aria-label={`Permission request: ${label}`}
      className="rounded border border-warning bg-warning-wash p-3 my-1"
      onKeyDown={onCardKeyDown}
    >
      <div className="mb-2 flex flex-wrap items-baseline gap-x-2 text-sm">
        <span className="font-medium text-ink">
          {block.plan ? 'Plan step approval' : 'Permission request'}
        </span>
        <span className="min-w-0 break-all font-mono text-xs text-ink-muted">{block.tool}</span>
      </div>

      {block.plan && commenting && planText ? (
        // The plan as numbered, commentable lines replaces the plain text (never shown twice).
        <Suspense fallback={<PlainDetail text={planText} />}>
          <div className="mb-3">
            <PlanStepLines text={planText} comments={steps} onChange={setSteps} />
          </div>
        </Suspense>
      ) : detail && <PlainDetail text={detail} />}

      {block.items && block.items.length > 0 && (
        <ul className="mb-3 list-disc pl-5 text-xs text-ink space-y-0.5">
          {block.items.map((item, i) => (
            <li key={i} className="break-words">{item}</li>
          ))}
        </ul>
      )}

      <div className="flex flex-col sm:flex-row sm:items-center gap-2" {...kb.containerProps}>
        {block.plan ? (
          <>
            <button type="button" {...kb.itemProps(0)} onClick={() => onDecision(block.permId, 'allow')}
              aria-label={`Approve & run ${label}`} className={primaryBtn}>
              Approve & run
            </button>
            <button type="button" tabIndex={keepItem.tabIndex}
              ref={(el) => { keepItem.ref(el); keepPlanningRef.current = el }}
              onClick={() => activate('keepPlanning')}
              aria-label={`Keep planning ${label}`} aria-expanded={commenting}
              aria-controls={commenting ? commentId : undefined} className={secondaryBtn}>
              Keep planning
            </button>
          </>
        ) : (
          <>
            <button type="button" {...kb.itemProps(0)} onClick={() => onDecision(block.permId, 'allow')}
              aria-label={`Allow ${label}`} className={allowBtn}>
              Allow once
            </button>
            {canSessionGrant && (
              <button type="button" {...kb.itemProps(1)} onClick={() => activate('session')}
                aria-label={`Allow for this session: ${label}`} className={secondaryBtn}
                title="Allow this for the rest of the session">
                Allow for this session
              </button>
            )}
            {rules && (
              <button type="button" {...kb.itemProps(actions.indexOf('chooser'))} onClick={() => activate('chooser')}
                aria-label={`Always allow ${label}`} className={tertiaryBtn}
                title="Choose a standing rule">
                Always allow…
              </button>
            )}
            <button type="button" {...kb.itemProps(actions.length - 1)} onClick={() => activate('deny')}
              aria-label={`Deny ${label}`} className={cn(secondaryBtn, 'sm:ml-auto')}>
              Deny
            </button>
          </>
        )}
      </div>

      {block.plan && commenting && (
        // Outside the roving-button row, and a TEXTAREA: the card's digit/arrow/A
        // handlers all ignore text-field targets, so typing here never answers the card.
        <div className="mt-3 flex flex-col gap-2">
          <label htmlFor={commentId} className="text-xs font-medium text-ink">
            What should change?
          </label>
          <textarea
            id={commentId}
            ref={commentRef}
            rows={3}
            placeholder="Optional"
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            onKeyDown={(e) => {
              // Esc closes the field; preventDefault keeps the shell's Esc (detach) off it.
              if (e.key === 'Escape') {
                e.preventDefault(); e.stopPropagation()
                cancelComment()
              }
            }}
            className={cn(
              'w-full resize-y rounded border border-edge-strong bg-surface px-2.5 py-2 text-sm text-ink',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
            )}
          />
          {/* The live region is always mounted and holds only the static "Too long", so it
              announces once as the limit is crossed — not the count on every keystroke. */}
          <p className={cn('text-xs font-medium text-danger', !tooLong && 'sr-only')}>
            <span role="status">{tooLong ? 'Too long' : ''}</span>
            {tooLong && <span>{` — ${feedback.length}/${PLAN_FEEDBACK_MAX}`}</span>}
          </p>
          <div className="flex flex-col sm:flex-row sm:items-center gap-2">
            <button type="button" onClick={sendKeepPlanning} disabled={tooLong}
              className={cn(secondaryBtn, 'disabled:cursor-not-allowed disabled:opacity-50')}>
              Send & keep planning
            </button>
            <button type="button" onClick={cancelComment} className={tertiaryBtn}>
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
    {rules && (
      // Outside the card div: a portaled dialog's key events bubble through the React tree,
      // and must not reach the card's digit/A shortcuts.
      <AlwaysAllowDialog
        open={chooserOpen}
        block={block}
        hasRepo={rules.hasRepo}
        initialScope={rules.hasRepo ? 'repo' : 'session'}
        onPreview={(pattern, scope) => rules.onPreviewRule(block.permId, pattern, scope)}
        onGrant={(pattern, scope, confirm) => rules.onGrantRule(block.permId, pattern, scope, confirm)}
        onClose={() => setChooserOpen(false)}
      />
    )}
    </>
  )
}

/** The request's full text. Focusable so a keyboard user can scroll a long request; the label
 *  says what the region is, not just that it is a block of monospace. */
function PlainDetail({ text }: { text: string }) {
  return (
    <pre
      data-testid="permission-detail"
      tabIndex={0}
      aria-label="Full request"
      className={cn(
        'mb-3 max-h-48 overflow-y-auto overscroll-contain rounded border border-warning/40 bg-surface',
        'px-2.5 py-2 font-mono text-xs leading-relaxed text-ink',
        'whitespace-pre-wrap break-all',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
      )}
    >
      {text}
    </pre>
  )
}
