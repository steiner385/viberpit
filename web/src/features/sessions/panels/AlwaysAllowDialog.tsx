import { useEffect, useState } from 'react'
import { Dialog } from '@/ui/Dialog'
import { RadioGroup } from '@/ui/RadioGroup'
import { Input } from '@/ui/Input'
import { Button } from '@/ui/Button'
import { ApiRequestError, apiErrorCode } from '@/api/client'
import type { RulePreview, RuleScope } from '@/api/types'
import type { Block } from '@/session/reducer'

type PermBlock = Extract<Block, { kind: 'permission' }>

interface Props {
  open: boolean
  block: PermBlock
  hasRepo: boolean
  onPreview: (pattern: string, scope: RuleScope) => Promise<RulePreview>
  /** Rejects with `ApiRequestError`; branch on its code. */
  onGrant: (pattern: string, scope: RuleScope, confirm: boolean) => Promise<unknown>
  onClose: () => void
}

const PREVIEW_DEBOUNCE_MS = 300

/** `{detail: {message, reasons}}` off an `ApiRequestError`; the server owns the copy. */
function errorDetail(err: unknown): { message: string; reasons: string[] } {
  const detail = err instanceof ApiRequestError ? (err.body.detail as Record<string, unknown> | undefined) : undefined
  const reasons = Array.isArray(detail?.reasons) ? (detail.reasons as string[]) : []
  const message = typeof detail?.message === 'string' ? detail.message : 'Could not save the rule'
  return { message, reasons }
}

function Chooser({ block, hasRepo, onPreview, onGrant, onClose }: Omit<Props, 'open'>) {
  const suggestions = block.ruleSuggestions ?? []
  const [pattern, setPattern] = useState(suggestions[0]?.pattern ?? '')
  const [scope, setScope] = useState<RuleScope>('session')
  const [rawPreview, setPreview] = useState<RulePreview | null>(null)
  // Set by a 409: the next explicit click is the one that sends confirm=true.
  const [confirmStep, setConfirmStep] = useState<string[] | null>(null)
  const [error, setError] = useState<{ code: string | null; message: string } | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!pattern.trim()) return
    let cancelled = false
    const t = setTimeout(() => {
      onPreview(pattern, scope).then(
        (p) => { if (!cancelled) setPreview(p) },
        () => { if (!cancelled) setPreview(null) },
      )
    }, PREVIEW_DEBOUNCE_MS)
    return () => { cancelled = true; clearTimeout(t) }
  }, [pattern, scope, onPreview])

  const edit = (next: () => void) => { next(); setConfirmStep(null); setError(null) }

  const preview = pattern.trim() ? rawPreview : null
  const refused = preview !== null && (!preview.valid || preview.level === 'refuse')
  const notCovered = preview?.coversRequest === false
  const needsConfirm = confirmStep !== null || preview?.needsConfirmation === true
  const stale = error?.code === 'PERMISSION_NOT_PENDING'
  const canGrant = pattern.trim() !== '' && !refused && !notCovered && !busy && !stale

  const grant = async () => {
    if (!canGrant) return
    setBusy(true)
    setError(null)
    try {
      await onGrant(pattern, scope, needsConfirm)
      onClose()
    } catch (err) {
      const code = apiErrorCode(err)
      const { message, reasons } = errorDetail(err)
      if (code === 'RULE_NEEDS_CONFIRMATION') setConfirmStep(reasons)
      else setError({ code, message })
    } finally {
      setBusy(false)
    }
  }

  const reasons = confirmStep && confirmStep.length > 0 ? confirmStep : (preview?.reasons ?? [])

  return (
    <div className="space-y-4">
      <RadioGroup
        name="aa-suggestion"
        legend="Suggested rules"
        value={suggestions.find((s) => s.pattern === pattern)?.pattern ?? ''}
        onChange={(p) => edit(() => setPattern(p))}
        options={suggestions.map((s) => ({
          value: s.pattern,
          label: s.pattern,
          description: s.description,
        }))}
      />
      <label className="block space-y-1 text-sm font-medium text-ink">
        Pattern
        <Input
          className="font-mono"
          value={pattern}
          onChange={(e) => edit(() => setPattern(e.target.value))}
          onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); void grant() } }}
          invalid={refused}
        />
      </label>
      <RadioGroup<RuleScope>
        name="aa-scope"
        legend="Applies to"
        value={scope}
        onChange={(s) => edit(() => setScope(s))}
        options={[
          { value: 'session', label: 'This session' },
          { value: 'repo', label: 'This repo', disabled: !hasRepo, disabledReason: 'Scratch session — no repo' },
          { value: 'global', label: 'All sessions' },
        ]}
      />
      <p className="text-xs text-ink-muted">Repo and All-sessions rules apply on every host and account.</p>

      <div className="space-y-1 text-sm" aria-live="polite">
        {preview?.description && <p className="text-ink">{preview.description}</p>}
        {reasons.map((r) => <p key={r} className="text-warning">{r}</p>)}
        {notCovered && <p className="text-warning">Doesn&apos;t cover this request</p>}
        {preview && preview.alsoPending > 0 && (
          <p className="text-ink-muted">
            Also resolves {preview.alsoPending} other waiting {preview.alsoPending === 1 ? 'request' : 'requests'}
          </p>
        )}
        {error && (
          <p role="alert" className="text-danger">
            {stale ? 'This request is no longer pending' : error.message}
          </p>
        )}
      </div>

      <div className="flex justify-end gap-2">
        {stale ? (
          <Button variant="secondary" onClick={onClose}>Close</Button>
        ) : (
          <>
            <Button variant="secondary" onClick={onClose}>Cancel</Button>
            <Button onClick={() => void grant()} disabled={!canGrant}>
              {!needsConfirm ? 'Grant' : scope === 'global' ? 'Confirm — allow everywhere' : 'Confirm'}
            </Button>
          </>
        )}
      </div>
    </div>
  )
}

/** Chooser for a standing "Always allow" rule. All I/O arrives through props. */
export function AlwaysAllowDialog({ open, ...rest }: Props) {
  return (
    <Dialog open={open} onClose={rest.onClose} title="Always allow">
      <Chooser {...rest} />
    </Dialog>
  )
}
