/**
 * The /api/notifications/* contract shared by the three sibling Settings
 * sections it backs (issue #602, split in #2077): Notifications (push + quiet
 * hours), Channels and Routing rules. Rules reads the channel list to name a
 * rule's target, so the query key and fetcher live here, once.
 */
import { useQuery } from '@tanstack/react-query'
import { cn } from '@/lib/utils'
import { api, ApiRequestError } from '@/api/client'

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

export interface QuietHours {
  start: string | null
  end: string | null
  // IANA zone (e.g. "America/New_York") start/end are local to. null = UTC (#2562).
  timezone: string | null
}

export interface TestDecision {
  action: string
  channelId: string | null
  deliver: boolean
  suppressedByQuiet: boolean
  matchedRuleId: string | null
}

export const NOTIF_KEYS = {
  quiet: ['notif-quiet'],
  channels: ['notif-channels'],
  rules: ['notif-rules'],
} as const

export function useNotificationChannels() {
  return useQuery({
    queryKey: NOTIF_KEYS.channels,
    queryFn: () => api.get<Channel[]>('/notifications/channels'),
  })
}

export const EVENT_CLASSES: { value: string; label: string }[] = [
  { value: '*', label: 'Any' },
  { value: 'needs_input', label: 'Needs input' },
  { value: 'session_ended', label: 'Session ended' },
  { value: 'automation_error', label: 'Automation error' },
  { value: 'automation_warn', label: 'Automation warning' },
  { value: 'pr_broke', label: 'PR broke' },
  { value: 'pr_merged', label: 'PR merged' },
  { value: 'deploy_failed', label: 'Deploy failed' },
  { value: 'account_limited', label: 'Account limited' },
  { value: 'decision_needed', label: 'Decision needed' },
]

export const ACTIONS: { value: string; label: string }[] = [
  { value: 'push', label: 'Push' },
  { value: 'channel', label: 'Channel' },
  { value: 'ignore', label: 'Ignore' },
]

export const eventLabel = (v: string) => EVENT_CLASSES.find((e) => e.value === v)?.label ?? v

export const inputClass = cn(
  'w-full rounded-md border border-edge-strong',
  'bg-surface text-ink',
  'px-3 py-2 text-sm min-h-target',
  'focus:outline-none focus:ring-2 focus:ring-accent',
  'placeholder:text-ink-faint',
)

export function errMessage(e: unknown): string {
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
