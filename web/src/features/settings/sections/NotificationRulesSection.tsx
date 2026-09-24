/**
 * NotificationRulesSection — which events notify, and how: push, a channel, or
 * nothing (issue #602). A sibling of Notifications and Channels since #2077.
 */
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { cn } from '@/lib/utils'
import { Button } from '@/ui/Button'
import { api } from '@/api/client'
import { SETTINGS_BY_ID } from '../settingsDefs'
import { SettingsSection } from '../chassis/SettingsSection'
import { SettingsGroup } from '../chassis/SettingsGroup'
import {
  ACTIONS, EVENT_CLASSES, NOTIF_KEYS, errMessage, eventLabel, inputClass, useNotificationChannels,
  type Channel, type Rule, type TestDecision,
} from './notificationsApi'

function scopeSummary(r: Rule): string {
  const parts: string[] = []
  if (r.scopeRepo) parts.push(`repo:${r.scopeRepo}`)
  if (r.scopeAccount) parts.push(`account:${r.scopeAccount}`)
  if (r.scopeAutomation) parts.push(`automation:${r.scopeAutomation}`)
  return parts.length ? parts.join(' · ') : 'any scope'
}

export function NotificationRulesSection() {
  const def = SETTINGS_BY_ID.rules
  const qc = useQueryClient()
  const { data: rules, isLoading } = useQuery({
    queryKey: NOTIF_KEYS.rules,
    queryFn: () => api.get<Rule[]>('/notifications/rules'),
  })
  const { data: channels } = useNotificationChannels()
  const channelName = (id: string | null) =>
    (id && channels?.find((c) => c.id === id)?.name) || id || '—'

  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      api.put<Rule>(`/notifications/rules/${id}`, { enabled }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: NOTIF_KEYS.rules }) },
  })
  const remove = useMutation({
    mutationFn: (id: string) => api.del<{ ok: true }>(`/notifications/rules/${id}`),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: NOTIF_KEYS.rules }) },
  })

  return (
    <SettingsSection id={def.id} title={def.label} description={def.description} scope={def.scope}>
      <SettingsGroup legend="Configured rules">
        <div className="py-3">
          {isLoading ? (
            <p className="text-sm text-ink-muted">Loading…</p>
          ) : rules && rules.length ? (
            <ul className="space-y-2">
              {rules.map((r) => (
                <li
                  key={r.id}
                  className="flex items-center gap-3 rounded-md border border-edge px-3 py-2"
                >
                  <div className="min-w-0 flex-1 space-y-0.5">
                    <div className="flex flex-wrap items-center gap-2 text-sm text-ink">
                      <span className="font-medium">{eventLabel(r.eventClass)}</span>
                      <span className="text-ink-muted">→</span>
                      <span>
                        {r.action}
                        {r.action === 'channel' && <span className="text-ink-muted"> ({channelName(r.channelId)})</span>}
                      </span>
                      {r.critical && (
                        <span className="rounded bg-warning-wash px-1.5 py-0.5 text-xs font-medium text-warning">
                          critical
                        </span>
                      )}
                    </div>
                    <div className="text-xs text-ink-muted">
                      {scopeSummary(r)} · priority {r.priority}
                    </div>
                  </div>
                  <label className="flex items-center gap-1 text-xs text-ink-muted">
                    <input
                      type="checkbox"
                      checked={r.enabled}
                      onChange={(e) => toggle.mutate({ id: r.id, enabled: e.target.checked })}
                      aria-label={`Enable rule ${r.id}`}
                      className="h-4 w-4 rounded border-edge-strong text-accent focus:ring-2 focus:ring-accent"
                    />
                    Enabled
                  </label>
                  <button
                    type="button"
                    onClick={() => remove.mutate(r.id)}
                    aria-label={`Delete rule ${r.id}`}
                    className="text-sm text-danger underline"
                  >
                    Delete
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-ink-muted">No rules yet.</p>
          )}
        </div>
      </SettingsGroup>

      <SettingsGroup legend="Add rule">
        <AddRuleForm qc={qc} channels={channels} channelName={channelName} />
      </SettingsGroup>
    </SettingsSection>
  )
}

function AddRuleForm({
  qc,
  channels,
  channelName,
}: {
  qc: ReturnType<typeof useQueryClient>
  channels: Channel[] | undefined
  channelName: (id: string | null) => string
}) {
  const [eventClass, setEventClass] = useState('*')
  const [action, setAction] = useState('push')
  const [channelId, setChannelId] = useState('')
  const [scopeRepo, setScopeRepo] = useState('')
  const [scopeAccount, setScopeAccount] = useState('')
  const [scopeAutomation, setScopeAutomation] = useState('')
  const [critical, setCritical] = useState(false)
  const [priority, setPriority] = useState(100)
  const [decision, setDecision] = useState<TestDecision | null>(null)

  const add = useMutation({
    mutationFn: (body: Record<string, unknown>) => api.post<Rule>('/notifications/rules', body),
    onSuccess: () => {
      setEventClass('*')
      setAction('push')
      setChannelId('')
      setScopeRepo('')
      setScopeAccount('')
      setScopeAutomation('')
      setCritical(false)
      setPriority(100)
      void qc.invalidateQueries({ queryKey: NOTIF_KEYS.rules })
    },
  })

  const test = useMutation({
    mutationFn: (body: Record<string, unknown>) => api.post<TestDecision>('/notifications/test', body),
    onSuccess: (d) => setDecision(d),
  })

  function scopeBody() {
    return {
      eventClass,
      ...(scopeRepo.trim() ? { repo: scopeRepo.trim() } : {}),
      ...(scopeAccount.trim() ? { account: scopeAccount.trim() } : {}),
      ...(scopeAutomation.trim() ? { automation: scopeAutomation.trim() } : {}),
    }
  }

  function handleAdd(e: React.FormEvent) {
    e.preventDefault()
    add.mutate({
      eventClass,
      action,
      ...(action === 'channel' ? { channelId } : {}),
      scopeRepo: scopeRepo.trim() || null,
      scopeAccount: scopeAccount.trim() || null,
      scopeAutomation: scopeAutomation.trim() || null,
      critical,
      priority,
    })
  }

  function handleTest() {
    setDecision(null)
    test.mutate(scopeBody())
  }

  return (
    <form onSubmit={handleAdd} className="space-y-3 py-3">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div className="space-y-1">
          <label htmlFor="rule-event" className="block text-xs text-ink-muted">Event</label>
          <select id="rule-event" value={eventClass} onChange={(e) => setEventClass(e.target.value)} className={inputClass}>
            {EVENT_CLASSES.map((e) => <option key={e.value} value={e.value}>{e.label}</option>)}
          </select>
        </div>
        <div className="space-y-1">
          <label htmlFor="rule-action" className="block text-xs text-ink-muted">Action</label>
          <select id="rule-action" value={action} onChange={(e) => setAction(e.target.value)} className={inputClass}>
            {ACTIONS.map((a) => <option key={a.value} value={a.value}>{a.label}</option>)}
          </select>
        </div>
      </div>

      {action === 'channel' && (
        <div className="space-y-1">
          <label htmlFor="rule-channel" className="block text-xs text-ink-muted">Channel</label>
          <select id="rule-channel" value={channelId} onChange={(e) => setChannelId(e.target.value)} className={inputClass}>
            <option value="">Select a channel…</option>
            {(channels ?? []).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <div className="space-y-1">
          <label htmlFor="rule-repo" className="block text-xs text-ink-muted">Repo <span className="text-ink-muted">(opt)</span></label>
          <input id="rule-repo" type="text" value={scopeRepo} onChange={(e) => setScopeRepo(e.target.value)} className={inputClass} />
        </div>
        <div className="space-y-1">
          <label htmlFor="rule-account" className="block text-xs text-ink-muted">Account <span className="text-ink-muted">(opt)</span></label>
          <input id="rule-account" type="text" value={scopeAccount} onChange={(e) => setScopeAccount(e.target.value)} className={inputClass} />
        </div>
        <div className="space-y-1">
          <label htmlFor="rule-automation" className="block text-xs text-ink-muted">Automation <span className="text-ink-muted">(opt)</span></label>
          <input id="rule-automation" type="text" value={scopeAutomation} onChange={(e) => setScopeAutomation(e.target.value)} className={inputClass} />
        </div>
      </div>

      <div className="flex flex-wrap items-end gap-4">
        <label className="flex items-center gap-2 text-sm text-ink">
          <input
            type="checkbox"
            checked={critical}
            onChange={(e) => setCritical(e.target.checked)}
            className="h-4 w-4 rounded border-edge-strong text-accent focus:ring-2 focus:ring-accent"
          />
          Critical (bypasses quiet hours)
        </label>
        <div className="space-y-1">
          <label htmlFor="rule-priority" className="block text-xs text-ink-muted">Priority</label>
          <input
            id="rule-priority"
            type="number"
            value={priority}
            onChange={(e) => setPriority(Number(e.target.value))}
            className={cn(inputClass, 'w-28')}
          />
        </div>
      </div>

      {add.isError && <p role="alert" className="text-sm text-danger">{errMessage(add.error)}</p>}

      <div className="flex flex-wrap justify-end gap-2">
        <Button type="button" variant="secondary" onClick={handleTest} disabled={test.isPending} className="min-h-target">
          {test.isPending ? 'Testing…' : 'Test routing'}
        </Button>
        <Button type="submit" disabled={add.isPending || (action === 'channel' && !channelId)} className="min-h-target">
          {add.isPending ? 'Adding…' : 'Add rule'}
        </Button>
      </div>

      {decision && (
        <div
          data-testid="test-decision"
          className="rounded-md bg-canvas border border-edge px-3 py-2 text-sm text-ink space-y-0.5"
        >
          <div>Action: <span className="font-medium">{decision.action}</span>
            {decision.action === 'channel' && decision.channelId && (
              <span className="text-ink-muted"> ({channelName(decision.channelId)})</span>
            )}
          </div>
          <div>Deliver: <span className="font-medium">{decision.deliver ? 'yes' : 'no'}</span></div>
          <div>Suppressed by quiet hours: <span className="font-medium">{decision.suppressedByQuiet ? 'yes' : 'no'}</span></div>
          <div>Matched rule: <span className="font-medium">{decision.matchedRuleId ?? 'none'}</span></div>
        </div>
      )}
    </form>
  )
}
