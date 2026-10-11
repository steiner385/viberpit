/**
 * PermissionRulesSection — list, edit and revoke the standing "Always allow…"
 * rules (spec 2026-10-11-always-allow-permission-rules §6). The server owns
 * matching, descriptions and risk; this only renders and forwards edits.
 */
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { ApiRequestError, apiErrorCode } from '@/api/client'
import { usePermissionRules, useUpdatePermissionRule, useRevokePermissionRule } from '@/api/hooks'
import type { RuleWire } from '@/api/types'
import { relTime, isValidTimestamp } from '@/lib/relativeTime'
import { Button } from '@/ui/Button'
import { Input } from '@/ui/Input'
import { useConfirm } from '@/ui/useConfirm'

interface Detail { message?: string; reasons?: string[] }

function detailOf(err: unknown): Detail {
  const body = err instanceof ApiRequestError ? err.body : null
  const d = body?.detail
  return typeof d === 'object' && d !== null ? (d as Detail) : {}
}

const when = (iso: string | null) => (isValidTimestamp(iso) ? relTime(iso) : 'never')

function RuleRow({ rule }: { rule: RuleWire }) {
  const update = useUpdatePermissionRule()
  const revoke = useRevokePermissionRule()
  const { confirm, dialog } = useConfirm()
  const [editing, setEditing] = useState(false)
  const [pattern, setPattern] = useState(rule.pattern)
  const [error, setError] = useState<string | null>(null)
  // Set => awaiting explicit confirm, but only for the exact pattern the server reviewed.
  const [needsConfirm, setNeedsConfirm] = useState<{ pattern: string; reasons: string[] } | null>(null)
  const reasons = needsConfirm && needsConfirm.pattern === pattern ? needsConfirm.reasons : null
  const [revokeError, setRevokeError] = useState<string | null>(null)

  const close = () => { setEditing(false); setError(null); setNeedsConfirm(null) }
  const save = async (confirmed: boolean) => {
    setError(null)
    const sent = pattern
    try {
      await update.mutateAsync(confirmed ? { id: rule.id, pattern: sent, confirm: true } : { id: rule.id, pattern: sent })
      close()
    } catch (err) {
      const d = detailOf(err)
      if (apiErrorCode(err) === 'RULE_NEEDS_CONFIRMATION') {
        setNeedsConfirm({ pattern: sent, reasons: d.reasons ?? [] })
        return
      }
      setNeedsConfirm(null)
      setError([d.message, ...(d.reasons ?? [])].filter(Boolean).join(' ') || 'Could not update the rule.')
    }
  }
  const onRevoke = async () => {
    const ok = await confirm({
      title: 'Revoke this rule?',
      description: 'Revoke this rule? Matching requests will ask again.',
      confirmLabel: 'Revoke',
      tone: 'danger',
    })
    if (!ok) return
    setRevokeError(null)
    try {
      await revoke.mutateAsync(rule.id)
    } catch (err) {
      setRevokeError(detailOf(err).message ?? 'Could not revoke the rule.')
    }
  }

  return (
    <li data-testid={`rule-${rule.id}`} className="rounded border border-edge px-3 py-2 text-sm space-y-1">
      {editing ? (
        <div className="space-y-2">
          <label className="flex flex-col gap-0.5">
            <span className="text-xs opacity-70">Pattern</span>
            <Input
              className="font-mono"
              value={pattern}
              disabled={update.isPending}
              onChange={(e) => { setPattern(e.target.value); setNeedsConfirm(null) }}
            />
          </label>
          {reasons && (
            <ul className="text-xs text-warning list-disc pl-4">
              {reasons.map((r) => <li key={r}>{r}</li>)}
            </ul>
          )}
          {error && <p role="alert" className="text-xs text-danger">{error}</p>}
          <div className="flex gap-2">
            <Button size="sm" disabled={update.isPending || !pattern.trim()} onClick={() => void save(reasons !== null)}>
              {reasons !== null ? 'Save anyway' : 'Save'}
            </Button>
            <Button size="sm" variant="secondary" onClick={close}>Cancel</Button>
          </div>
        </div>
      ) : (
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <code className="font-mono break-all">{rule.pattern}</code>
            <div className="text-xs opacity-70">{rule.description}</div>
            <div className="text-xs opacity-60">by {rule.createdBy} · {when(rule.createdAt)}</div>
            <div className="text-xs opacity-60">{rule.hitCount} hits · last {when(rule.lastHitAt)}</div>
          </div>
          <div className="flex gap-1 shrink-0">
            <Button size="sm" variant="secondary" onClick={() => { setPattern(rule.pattern); setEditing(true) }}>Edit</Button>
            <Button size="sm" variant="secondary" onClick={() => void onRevoke()}>Revoke</Button>
          </div>
        </div>
      )}
      {revokeError && <p role="alert" className="text-xs text-danger">{revokeError}</p>}
      {dialog}
    </li>
  )
}

function Group({ title, rules }: { title: React.ReactNode; rules: RuleWire[] }) {
  if (rules.length === 0) return null
  return (
    <section className="space-y-1">
      <h4 className="text-xs font-semibold uppercase tracking-wide opacity-70">{title}</h4>
      <ul className="space-y-1">{rules.map((r) => <RuleRow key={r.id} rule={r} />)}</ul>
    </section>
  )
}

export function PermissionRulesSection() {
  const { data: rules = [], isLoading, isError } = usePermissionRules()
  if (isLoading) return <div className="text-sm opacity-60">Loading rules…</div>
  if (isError) return <p role="alert" className="text-sm text-danger">Could not load the rules.</p>

  const global = rules.filter((r) => r.scope === 'global')
  const sessions = rules.filter((r) => r.scope === 'session')
  const repos = new Map<string, RuleWire[]>()
  for (const r of rules.filter((x) => x.scope === 'repo')) {
    const key = r.scopeKey ?? ''
    repos.set(key, [...(repos.get(key) ?? []), r])
  }

  return (
    <div className="space-y-4">
      <p className="text-xs opacity-70">
        Repo and All-sessions rules apply on every host and account. Only a person can create one — never the assistant or automations.
      </p>
      {rules.length === 0 ? (
        <p className="text-sm opacity-60">
          No standing rules yet. Choose <strong>Always allow…</strong> on a permission card to create one.
        </p>
      ) : (
        <>
          <Group title="All sessions" rules={global} />
          {repos.size > 0 && (
            <section className="space-y-2">
              <h4 className="text-xs font-semibold uppercase tracking-wide opacity-70">Repos</h4>
              {[...repos].map(([key, rs]) => (
                <div key={key} className="space-y-1 pl-2">
                  <h5 className="text-xs font-mono opacity-80">{key}</h5>
                  <ul className="space-y-1">{rs.map((r) => <RuleRow key={r.id} rule={r} />)}</ul>
                </div>
              ))}
            </section>
          )}
          {sessions.length > 0 && (
            <section className="space-y-2">
              <h4 className="text-xs font-semibold uppercase tracking-wide opacity-70">Sessions</h4>
              {sessions.map((r) => (
                <div key={r.id} className="space-y-1 pl-2">
                  {r.scopeKey && (
                    <h5 className="text-xs">
                      <Link to={`/sessions/${r.scopeKey}`} className="font-mono text-accent hover:underline">{r.scopeKey.slice(0, 8)}</Link>
                    </h5>
                  )}
                  <ul><RuleRow rule={r} /></ul>
                </div>
              ))}
            </section>
          )}
        </>
      )}
    </div>
  )
}
