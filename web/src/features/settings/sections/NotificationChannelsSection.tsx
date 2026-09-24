/**
 * NotificationChannelsSection — the webhook delivery channels a routing rule
 * can target (issue #602). A sibling of Notifications since #2077.
 */
import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { cn } from '@/lib/utils'
import { Button } from '@/ui/Button'
import { api } from '@/api/client'
import { SETTINGS_BY_ID } from '../settingsDefs'
import { SettingsSection } from '../chassis/SettingsSection'
import { SettingsGroup } from '../chassis/SettingsGroup'
import { NOTIF_KEYS, errMessage, inputClass, useNotificationChannels, type Channel } from './notificationsApi'

export function NotificationChannelsSection() {
  const def = SETTINGS_BY_ID.channels
  const qc = useQueryClient()
  const { data: channels, isLoading } = useNotificationChannels()

  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      api.put<Channel>(`/notifications/channels/${id}`, { enabled }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: NOTIF_KEYS.channels }) },
  })
  const remove = useMutation({
    mutationFn: (id: string) => api.del<{ ok: true }>(`/notifications/channels/${id}`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: NOTIF_KEYS.channels })
      // Deleting a channel cascades to the rules that target it.
      void qc.invalidateQueries({ queryKey: NOTIF_KEYS.rules })
    },
  })

  return (
    <SettingsSection id={def.id} title={def.label} description={def.description} scope={def.scope}>
      <SettingsGroup legend="Configured channels">
        <div className="py-3">
          {isLoading ? (
            <p className="text-sm text-ink-muted">Loading…</p>
          ) : channels && channels.length ? (
            <ul className="space-y-2">
              {channels.map((c) => (
                <li
                  key={c.id}
                  className="flex items-center gap-3 rounded-md border border-edge px-3 py-2"
                >
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-medium text-ink truncate">{c.name}</div>
                    <div className="text-xs text-ink-muted truncate">{c.url}</div>
                  </div>
                  <label className="flex items-center gap-1 text-xs text-ink-muted">
                    <input
                      type="checkbox"
                      checked={c.enabled}
                      onChange={(e) => toggle.mutate({ id: c.id, enabled: e.target.checked })}
                      aria-label={`Enable ${c.name}`}
                      className="h-4 w-4 rounded border-edge-strong text-accent focus:ring-2 focus:ring-accent"
                    />
                    Enabled
                  </label>
                  <button
                    type="button"
                    onClick={() => remove.mutate(c.id)}
                    aria-label={`Delete ${c.name}`}
                    className="text-sm text-danger underline"
                  >
                    Delete
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-ink-muted">No channels yet.</p>
          )}
        </div>
      </SettingsGroup>

      <SettingsGroup legend="Add channel">
        <AddChannelForm qc={qc} />
      </SettingsGroup>
    </SettingsSection>
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
      void qc.invalidateQueries({ queryKey: NOTIF_KEYS.channels })
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
    <form onSubmit={handleSubmit} className="space-y-2 py-3">
      <div className="space-y-1">
        <label htmlFor="channel-name" className="block text-xs text-ink-muted">Name</label>
        <input id="channel-name" type="text" value={name} onChange={(e) => setName(e.target.value)} className={inputClass} />
      </div>
      <div className="space-y-1">
        <label htmlFor="channel-url" className="block text-xs text-ink-muted">URL</label>
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
        <label htmlFor="channel-headers" className="block text-xs text-ink-muted">
          Headers <span className="text-ink-muted">(optional JSON)</span>
        </label>
        <textarea
          id="channel-headers"
          value={headers}
          onChange={(e) => setHeaders(e.target.value)}
          rows={3}
          placeholder='{"Authorization": "Bearer …"}'
          className={cn(inputClass, 'font-mono min-h-18')}
        />
      </div>
      {parseError && <p role="alert" className="text-sm text-danger">{parseError}</p>}
      {add.isError && <p role="alert" className="text-sm text-danger">{errMessage(add.error)}</p>}
      <div className="flex justify-end">
        <Button type="submit" disabled={add.isPending || !name.trim() || !url.trim()} className="min-h-target">
          {add.isPending ? 'Adding…' : 'Add channel'}
        </Button>
      </div>
    </form>
  )
}
