/**
 * NotificationsSection — the notification-routing settings surface: web-push
 * enable, quiet hours, delivery channels, and routing rules (issue #602).
 * Uses react-query inline (no shared hooks) against the /api/notifications/* API.
 */
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { cn } from '@/lib/utils'
import { Button } from '@/ui/Button'
import { api, ApiRequestError } from '@/api/client'
import { NotificationsToggle } from './NotificationsToggle'

// ---- Types (mirror the backend contracts) --------------------------------

export interface Channel {
  id: string
  name: string
  kind: string
  url: string
  headers: Record<string, string> | null
  enabled: boolean
  createdAt: string
}

export interface Rule {
  id: string
  enabled: boolean
  eventClass: string
  scopeRepo: string | null
  scopeAccount: string | null
  scopeAutomation: string | null
  action: string
  channelId: string | null
  critical: boolean
  priority: number
  createdAt: string
}

interface QuietHours {
  start: string | null
  end: string | null
}

interface TestDecision {
  action: string
  channelId: string | null
  deliver: boolean
  suppressedByQuiet: boolean
  matchedRuleId: string | null
}

const EVENT_CLASSES: { value: string; label: string }[] = [
  { value: '*', label: 'Any' },
  { value: 'needs_input', label: 'Needs input' },
  { value: 'session_ended', label: 'Session ended' },
  { value: 'automation_error', label: 'Automation error' },
  { value: 'automation_warn', label: 'Automation warning' },
  { value: 'pr_broke', label: 'PR broke' },
  { value: 'pr_merged', label: 'PR merged' },
]

const ACTIONS: { value: string; label: string }[] = [
  { value: 'push', label: 'Push' },
  { value: 'channel', label: 'Channel' },
  { value: 'ignore', label: 'Ignore' },
]

const eventLabel = (v: string) => EVENT_CLASSES.find((e) => e.value === v)?.label ?? v

const inputClass = cn(
  'w-full rounded-md border border-gray-300 dark:border-gray-600',
  'bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100',
  'px-3 py-2 text-sm min-h-[44px]',
  'focus:outline-none focus:ring-2 focus:ring-blue-500',
  'placeholder:text-gray-400 dark:placeholder:text-gray-500',
)

function errMessage(e: unknown): string {
  if (e instanceof ApiRequestError) {
    const body = e.body
    const detail = body?.detail
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail) && detail.length && typeof detail[0] === 'object') {
      const msg = (detail[0] as Record<string, unknown>).msg
      if (typeof msg === 'string') return msg
    }
    return `Request failed (${e.status})`
  }
  return e instanceof Error ? e.message : String(e)
}

// ---- Root ----------------------------------------------------------------

export function NotificationsSection() {
  return (
    <div className="space-y-8 max-w-2xl">
      <section className="space-y-3">
        <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">Phone notifications</h3>
        <NotificationsToggle />
      </section>

      <QuietHoursSection />
      <ChannelsSection />
      <RulesSection />
    </div>
  )
}

// ---- Quiet hours ---------------------------------------------------------

function QuietHoursSection() {
  const qc = useQueryClient()
  const { data, isLoading } = useQuery({
    queryKey: ['notif-quiet'],
    queryFn: () => api.get<QuietHours>('/notifications/quiet-hours'),
  })

  return (
    <section className="space-y-3 border-t border-gray-200 dark:border-gray-700 pt-6">
      <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">Quiet hours</h3>
      {isLoading ? (
        <p className="text-sm text-gray-500">Loading…</p>
      ) : (
        <QuietHoursForm key={`${data?.start}-${data?.end}`} initial={data} qc={qc} />
      )}
    </section>
  )
}

function QuietHoursForm({ initial, qc }: { initial: QuietHours | undefined; qc: ReturnType<typeof useQueryClient> }) {
  const [start, setStart] = useState(initial?.start ?? '')
  const [end, setEnd] = useState(initial?.end ?? '')

  const save = useMutation({
    mutationFn: (body: QuietHours) => api.put<QuietHours>('/notifications/quiet-hours', body),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ['notif-quiet'] }) },
  })

  function handleSave() {
    save.mutate({ start: start || null, end: end || null })
  }
  function handleClear() {
    setStart('')
    setEnd('')
    save.mutate({ start: null, end: null })
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-3">
        <div className="space-y-1">
          <label htmlFor="quiet-start" className="block text-sm font-medium text-gray-700 dark:text-gray-300">Start</label>
          <input
            id="quiet-start"
            type="time"
            value={start}
            onChange={(e) => setStart(e.target.value)}
            className={cn(inputClass, 'w-auto')}
          />
        </div>
        <div className="space-y-1">
          <label htmlFor="quiet-end" className="block text-sm font-medium text-gray-700 dark:text-gray-300">End</label>
          <input
            id="quiet-end"
            type="time"
            value={end}
            onChange={(e) => setEnd(e.target.value)}
            className={cn(inputClass, 'w-auto')}
          />
        </div>
        <Button type="button" onClick={handleSave} disabled={save.isPending} className="min-h-[44px]">
          {save.isPending ? 'Saving…' : 'Save'}
        </Button>
        <Button type="button" variant="outline" onClick={handleClear} disabled={save.isPending} className="min-h-[44px]">
          Clear
        </Button>
      </div>
      <p className="text-xs text-gray-500 dark:text-gray-400">Rules marked &lsquo;critical&rsquo; bypass quiet hours.</p>
      {save.isError && (
        <p role="alert" className="text-sm text-destructive">{errMessage(save.error)}</p>
      )}
    </div>
  )
}

// ---- Channels ------------------------------------------------------------

function ChannelsSection() {
  const qc = useQueryClient()
  const { data: channels, isLoading } = useQuery({
    queryKey: ['notif-channels'],
    queryFn: () => api.get<Channel[]>('/notifications/channels'),
  })

  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      api.put<Channel>(`/notifications/channels/${id}`, { enabled }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ['notif-channels'] }) },
  })
  const remove = useMutation({
    mutationFn: (id: string) => api.del<{ ok: true }>(`/notifications/channels/${id}`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['notif-channels'] })
      void qc.invalidateQueries({ queryKey: ['notif-rules'] })
    },
  })

  return (
    <section className="space-y-3 border-t border-gray-200 dark:border-gray-700 pt-6">
      <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">Channels</h3>
      {isLoading ? (
        <p className="text-sm text-gray-500">Loading…</p>
      ) : channels && channels.length ? (
        <ul className="space-y-2">
          {channels.map((c) => (
            <li
              key={c.id}
              className="flex items-center gap-3 rounded-md border border-gray-200 dark:border-gray-700 px-3 py-2"
            >
              <div className="min-w-0 flex-1">
                <div className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">{c.name}</div>
                <div className="text-xs text-gray-500 dark:text-gray-400 truncate">{c.url}</div>
              </div>
              <label className="flex items-center gap-1 text-xs text-gray-600 dark:text-gray-300">
                <input
                  type="checkbox"
                  checked={c.enabled}
                  onChange={(e) => toggle.mutate({ id: c.id, enabled: e.target.checked })}
                  aria-label={`Enable ${c.name}`}
                  className="h-4 w-4 rounded border-gray-300 dark:border-gray-600 text-blue-600 focus:ring-2 focus:ring-blue-500"
                />
                Enabled
              </label>
              <button
                type="button"
                onClick={() => remove.mutate(c.id)}
                aria-label={`Delete ${c.name}`}
                className="text-sm text-destructive underline"
              >
                Delete
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-gray-500 dark:text-gray-400">No channels yet.</p>
      )}
      <AddChannelForm qc={qc} />
    </section>
  )
}

function AddChannelForm({ qc }: { qc: ReturnType<typeof useQueryClient> }) {
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [headers, setHeaders] = useState('')
  const [parseError, setParseError] = useState<string | null>(null)

  const add = useMutation({
    mutationFn: (body: { name: string; url: string; headers?: Record<string, unknown> }) =>
      api.post<Channel>('/notifications/channels', body),
    onSuccess: () => {
      setName('')
      setUrl('')
      setHeaders('')
      setParseError(null)
      void qc.invalidateQueries({ queryKey: ['notif-channels'] })
    },
  })

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setParseError(null)
    let parsed: Record<string, unknown> | undefined
    const trimmed = headers.trim()
    if (trimmed) {
      try {
        const obj = JSON.parse(trimmed)
        if (obj === null || typeof obj !== 'object' || Array.isArray(obj)) {
          setParseError('Headers must be a JSON object.')
          return
        }
        parsed = obj as Record<string, unknown>
      } catch {
        setParseError('Headers are not valid JSON.')
        return
      }
    }
    add.mutate({ name: name.trim(), url: url.trim(), ...(parsed ? { headers: parsed } : {}) })
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-2 rounded-md border border-dashed border-gray-300 dark:border-gray-600 p-3">
      <div className="text-sm font-medium text-gray-700 dark:text-gray-300">Add channel</div>
      <div className="space-y-1">
        <label htmlFor="channel-name" className="block text-xs text-gray-600 dark:text-gray-400">Name</label>
        <input id="channel-name" type="text" value={name} onChange={(e) => setName(e.target.value)} className={inputClass} />
      </div>
      <div className="space-y-1">
        <label htmlFor="channel-url" className="block text-xs text-gray-600 dark:text-gray-400">URL</label>
        <input
          id="channel-url"
          type="text"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://example.com/webhook"
          className={inputClass}
        />
      </div>
      <div className="space-y-1">
        <label htmlFor="channel-headers" className="block text-xs text-gray-600 dark:text-gray-400">
          Headers <span className="text-gray-400">(optional JSON)</span>
        </label>
        <textarea
          id="channel-headers"
          value={headers}
          onChange={(e) => setHeaders(e.target.value)}
          rows={3}
          placeholder='{"Authorization": "Bearer …"}'
          className={cn(inputClass, 'font-mono min-h-[72px]')}
        />
      </div>
      {parseError && <p role="alert" className="text-sm text-destructive">{parseError}</p>}
      {add.isError && <p role="alert" className="text-sm text-destructive">{errMessage(add.error)}</p>}
      <div className="flex justify-end">
        <Button type="submit" disabled={add.isPending || !name.trim() || !url.trim()} className="min-h-[44px]">
          {add.isPending ? 'Adding…' : 'Add channel'}
        </Button>
      </div>
    </form>
  )
}

// ---- Rules ---------------------------------------------------------------

function scopeSummary(r: Rule): string {
  const parts: string[] = []
  if (r.scopeRepo) parts.push(`repo:${r.scopeRepo}`)
  if (r.scopeAccount) parts.push(`account:${r.scopeAccount}`)
  if (r.scopeAutomation) parts.push(`automation:${r.scopeAutomation}`)
  return parts.length ? parts.join(' · ') : 'any scope'
}

function RulesSection() {
  const qc = useQueryClient()
  const { data: rules, isLoading } = useQuery({
    queryKey: ['notif-rules'],
    queryFn: () => api.get<Rule[]>('/notifications/rules'),
  })
  const { data: channels } = useQuery({
    queryKey: ['notif-channels'],
    queryFn: () => api.get<Channel[]>('/notifications/channels'),
  })
  const channelName = (id: string | null) =>
    (id && channels?.find((c) => c.id === id)?.name) || id || '—'

  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      api.put<Rule>(`/notifications/rules/${id}`, { enabled }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ['notif-rules'] }) },
  })
  const remove = useMutation({
    mutationFn: (id: string) => api.del<{ ok: true }>(`/notifications/rules/${id}`),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ['notif-rules'] }) },
  })

  return (
    <section className="space-y-3 border-t border-gray-200 dark:border-gray-700 pt-6">
      <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">Rules</h3>
      {isLoading ? (
        <p className="text-sm text-gray-500">Loading…</p>
      ) : rules && rules.length ? (
        <ul className="space-y-2">
          {rules.map((r) => (
            <li
              key={r.id}
              className="flex items-center gap-3 rounded-md border border-gray-200 dark:border-gray-700 px-3 py-2"
            >
              <div className="min-w-0 flex-1 space-y-0.5">
                <div className="flex flex-wrap items-center gap-2 text-sm text-gray-900 dark:text-gray-100">
                  <span className="font-medium">{eventLabel(r.eventClass)}</span>
                  <span className="text-gray-400">→</span>
                  <span>
                    {r.action}
                    {r.action === 'channel' && <span className="text-gray-500"> ({channelName(r.channelId)})</span>}
                  </span>
                  {r.critical && (
                    <span className="rounded bg-amber-100 dark:bg-amber-900/40 px-1.5 py-0.5 text-xs font-medium text-amber-800 dark:text-amber-300">
                      critical
                    </span>
                  )}
                </div>
                <div className="text-xs text-gray-500 dark:text-gray-400">
                  {scopeSummary(r)} · priority {r.priority}
                </div>
              </div>
              <label className="flex items-center gap-1 text-xs text-gray-600 dark:text-gray-300">
                <input
                  type="checkbox"
                  checked={r.enabled}
                  onChange={(e) => toggle.mutate({ id: r.id, enabled: e.target.checked })}
                  aria-label={`Enable rule ${r.id}`}
                  className="h-4 w-4 rounded border-gray-300 dark:border-gray-600 text-blue-600 focus:ring-2 focus:ring-blue-500"
                />
                Enabled
              </label>
              <button
                type="button"
                onClick={() => remove.mutate(r.id)}
                aria-label={`Delete rule ${r.id}`}
                className="text-sm text-destructive underline"
              >
                Delete
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-gray-500 dark:text-gray-400">No rules yet.</p>
      )}
      <AddRuleForm qc={qc} channels={channels} channelName={channelName} />
    </section>
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
      void qc.invalidateQueries({ queryKey: ['notif-rules'] })
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
    <form onSubmit={handleAdd} className="space-y-3 rounded-md border border-dashed border-gray-300 dark:border-gray-600 p-3">
      <div className="text-sm font-medium text-gray-700 dark:text-gray-300">Add rule</div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div className="space-y-1">
          <label htmlFor="rule-event" className="block text-xs text-gray-600 dark:text-gray-400">Event</label>
          <select id="rule-event" value={eventClass} onChange={(e) => setEventClass(e.target.value)} className={inputClass}>
            {EVENT_CLASSES.map((e) => <option key={e.value} value={e.value}>{e.label}</option>)}
          </select>
        </div>
        <div className="space-y-1">
          <label htmlFor="rule-action" className="block text-xs text-gray-600 dark:text-gray-400">Action</label>
          <select id="rule-action" value={action} onChange={(e) => setAction(e.target.value)} className={inputClass}>
            {ACTIONS.map((a) => <option key={a.value} value={a.value}>{a.label}</option>)}
          </select>
        </div>
      </div>

      {action === 'channel' && (
        <div className="space-y-1">
          <label htmlFor="rule-channel" className="block text-xs text-gray-600 dark:text-gray-400">Channel</label>
          <select id="rule-channel" value={channelId} onChange={(e) => setChannelId(e.target.value)} className={inputClass}>
            <option value="">Select a channel…</option>
            {(channels ?? []).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <div className="space-y-1">
          <label htmlFor="rule-repo" className="block text-xs text-gray-600 dark:text-gray-400">Repo <span className="text-gray-400">(opt)</span></label>
          <input id="rule-repo" type="text" value={scopeRepo} onChange={(e) => setScopeRepo(e.target.value)} className={inputClass} />
        </div>
        <div className="space-y-1">
          <label htmlFor="rule-account" className="block text-xs text-gray-600 dark:text-gray-400">Account <span className="text-gray-400">(opt)</span></label>
          <input id="rule-account" type="text" value={scopeAccount} onChange={(e) => setScopeAccount(e.target.value)} className={inputClass} />
        </div>
        <div className="space-y-1">
          <label htmlFor="rule-automation" className="block text-xs text-gray-600 dark:text-gray-400">Automation <span className="text-gray-400">(opt)</span></label>
          <input id="rule-automation" type="text" value={scopeAutomation} onChange={(e) => setScopeAutomation(e.target.value)} className={inputClass} />
        </div>
      </div>

      <div className="flex flex-wrap items-end gap-4">
        <label className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
          <input
            type="checkbox"
            checked={critical}
            onChange={(e) => setCritical(e.target.checked)}
            className="h-4 w-4 rounded border-gray-300 dark:border-gray-600 text-blue-600 focus:ring-2 focus:ring-blue-500"
          />
          Critical (bypasses quiet hours)
        </label>
        <div className="space-y-1">
          <label htmlFor="rule-priority" className="block text-xs text-gray-600 dark:text-gray-400">Priority</label>
          <input
            id="rule-priority"
            type="number"
            value={priority}
            onChange={(e) => setPriority(Number(e.target.value))}
            className={cn(inputClass, 'w-28')}
          />
        </div>
      </div>

      {add.isError && <p role="alert" className="text-sm text-destructive">{errMessage(add.error)}</p>}

      <div className="flex flex-wrap justify-end gap-2">
        <Button type="button" variant="outline" onClick={handleTest} disabled={test.isPending} className="min-h-[44px]">
          {test.isPending ? 'Testing…' : 'Test routing'}
        </Button>
        <Button type="submit" disabled={add.isPending || (action === 'channel' && !channelId)} className="min-h-[44px]">
          {add.isPending ? 'Adding…' : 'Add rule'}
        </Button>
      </div>

      {decision && (
        <div
          data-testid="test-decision"
          className="rounded-md bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 px-3 py-2 text-sm text-gray-700 dark:text-gray-300 space-y-0.5"
        >
          <div>Action: <span className="font-medium">{decision.action}</span>
            {decision.action === 'channel' && decision.channelId && (
              <span className="text-gray-500"> ({channelName(decision.channelId)})</span>
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
