import { useEffect, useMemo, useRef, useState } from 'react'
import { api, ApiRequestError } from '@/api/client'
import { cn } from '@/lib/utils'
import { Button } from '@/ui/Button'

// --- Schedule shape ----------------------------------------------------------

export interface ActiveHours {
  start?: string // "HH:MM"
  end?: string // "HH:MM"
  days?: number[] // 0=Mon .. 6=Sun; empty/absent = every day
}

export interface Schedule {
  activeHours?: ActiveHours
  cron?: string
}

export interface ScheduleEditorProps {
  /** Current stored schedule ({activeHours?, cron?} or null). */
  schedule: Record<string, unknown> | null
  /** Called with the new schedule ({} clears it); parent PATCHes the automation. */
  onSave: (schedule: Record<string, unknown>) => void
  saving?: boolean
}

interface PreviewResponse {
  nextRuns: string[]
  presets: Record<string, unknown>
}

// --- Presets -----------------------------------------------------------------

interface PresetDef {
  key: string
  label: string
  schedule: Schedule | null // null == "Custom…" (no canned schedule)
}

const PRESETS: PresetDef[] = [
  { key: 'always', label: 'Always (no restriction)', schedule: {} },
  {
    key: 'weekdays',
    label: 'Weekdays 9–18',
    schedule: { activeHours: { start: '09:00', end: '18:00', days: [0, 1, 2, 3, 4] } },
  },
  { key: 'daily8', label: 'Daily 8am', schedule: { cron: '0 8 * * *' } },
  { key: 'hourly', label: 'Hourly', schedule: { cron: '0 * * * *' } },
  { key: 'every15', label: 'Every 15 min', schedule: { cron: '*/15 * * * *' } },
  { key: 'custom', label: 'Custom…', schedule: null },
]

const DAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

// --- Helpers -----------------------------------------------------------------

/** Normalize an incoming stored schedule (or null) into a Schedule object. */
function normalize(raw: Record<string, unknown> | null): Schedule {
  if (!raw || typeof raw !== 'object') return {}
  const out: Schedule = {}
  const ah = (raw as { activeHours?: unknown }).activeHours
  if (ah && typeof ah === 'object') {
    const a = ah as Record<string, unknown>
    const parsed: ActiveHours = {}
    if (typeof a.start === 'string') parsed.start = a.start
    if (typeof a.end === 'string') parsed.end = a.end
    if (Array.isArray(a.days)) parsed.days = a.days.filter((d): d is number => typeof d === 'number')
    out.activeHours = parsed
  }
  const cron = (raw as { cron?: unknown }).cron
  if (typeof cron === 'string' && cron.trim() !== '') out.cron = cron
  return out
}

/** Build the schedule object to preview/save from the working UI state. */
function buildSchedule(activeHours: ActiveHours, cron: string): Schedule {
  const out: Schedule = {}
  const hasHours = Boolean(activeHours.start || activeHours.end || (activeHours.days && activeHours.days.length))
  if (hasHours) {
    const ah: ActiveHours = {}
    if (activeHours.start) ah.start = activeHours.start
    if (activeHours.end) ah.end = activeHours.end
    if (activeHours.days && activeHours.days.length) ah.days = [...activeHours.days].sort((a, b) => a - b)
    out.activeHours = ah
  }
  const trimmed = cron.trim()
  if (trimmed) out.cron = trimmed
  return out
}

/** Match a schedule to a preset key; falls back to 'custom'. */
function matchPreset(schedule: Schedule): string {
  const canon = JSON.stringify(buildSchedule(schedule.activeHours ?? {}, schedule.cron ?? ''))
  for (const p of PRESETS) {
    if (p.schedule === null) continue
    if (JSON.stringify(p.schedule) === canon) return p.key
  }
  return 'custom'
}

function detailFrom(body: Record<string, unknown>): string {
  const d = body.detail
  if (typeof d === 'string') return d
  if (Array.isArray(d) && d.length && typeof d[0] === 'object' && d[0] && 'msg' in d[0]) {
    return String((d[0] as { msg: unknown }).msg)
  }
  return 'Invalid schedule.'
}

// --- Component ---------------------------------------------------------------

export function ScheduleEditor({ schedule, onSave, saving = false }: ScheduleEditorProps) {
  const initial = useMemo(() => normalize(schedule), [schedule])

  const [preset, setPreset] = useState<string>(() => matchPreset(initial))
  const [activeHours, setActiveHours] = useState<ActiveHours>(() => initial.activeHours ?? {})
  const [cron, setCron] = useState<string>(() => initial.cron ?? '')

  const [nextRuns, setNextRuns] = useState<string[]>([])
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [previewing, setPreviewing] = useState(false)

  const current = useMemo(() => buildSchedule(activeHours, cron), [activeHours, cron])
  const hasCron = Boolean(current.cron)
  const currentKey = useMemo(() => JSON.stringify(current), [current])

  // Live preview: debounced POST to the preview endpoint.
  const reqId = useRef(0)
  useEffect(() => {
    const mine = ++reqId.current
    const t = setTimeout(() => {
      setPreviewing(true)
      api
        .post<PreviewResponse>('/automations/schedule/preview', { schedule: current })
        .then((res) => {
          if (mine !== reqId.current) return
          setNextRuns(Array.isArray(res.nextRuns) ? res.nextRuns : [])
          setPreviewError(null)
        })
        .catch((err: unknown) => {
          if (mine !== reqId.current) return
          setNextRuns([])
          if (err instanceof ApiRequestError && err.status === 422) {
            setPreviewError(detailFrom(err.body))
          } else {
            setPreviewError(err instanceof Error ? err.message : 'Preview failed.')
          }
        })
        .finally(() => {
          if (mine === reqId.current) setPreviewing(false)
        })
    }, 400)
    return () => clearTimeout(t)
    // currentKey is a stable serialization of `current`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentKey])

  function applyPreset(key: string) {
    setPreset(key)
    const def = PRESETS.find((p) => p.key === key)
    if (!def || def.schedule === null) return // "Custom…": keep current fields
    const next = normalize(def.schedule as Record<string, unknown>)
    setActiveHours(next.activeHours ?? {})
    setCron(next.cron ?? '')
  }

  function toggleDay(day: number) {
    setPreset('custom')
    setActiveHours((prev) => {
      const days = prev.days ?? []
      const next = days.includes(day) ? days.filter((d) => d !== day) : [...days, day]
      return { ...prev, days: next }
    })
  }

  function setStart(v: string) {
    setPreset('custom')
    setActiveHours((prev) => ({ ...prev, start: v }))
  }
  function setEnd(v: string) {
    setPreset('custom')
    setActiveHours((prev) => ({ ...prev, end: v }))
  }
  function setCronValue(v: string) {
    setPreset('custom')
    setCron(v)
  }

  const invalid = previewError !== null
  const isAlways = preset === 'always' || (Object.keys(current).length === 0)

  function handleSave() {
    // "Always" (or an empty working schedule) clears the schedule with {}.
    onSave(isAlways ? {} : (current as Record<string, unknown>))
  }

  const days = activeHours.days ?? []

  return (
    <section className="space-y-3">
      <h4 className="text-xs font-semibold uppercase text-ink-muted">Schedule</h4>

      {/* Preset */}
      <div>
        <label className="block text-sm font-medium mb-1" htmlFor="schedule-preset">
          Preset
        </label>
        <select
          id="schedule-preset"
          aria-label="Schedule preset"
          value={preset}
          onChange={(e) => applyPreset(e.target.value)}
          className="text-sm rounded border border-edge bg-canvas px-2 py-1 w-full min-h-[44px]"
        >
          {PRESETS.map((p) => (
            <option key={p.key} value={p.key}>{p.label}</option>
          ))}
        </select>
      </div>

      {/* Active hours */}
      <div>
        <span className="block text-sm font-medium mb-1">Active hours</span>
        <div className="flex items-center gap-2 flex-wrap">
          <label className="flex items-center gap-1 text-sm">
            <span className="text-ink-muted">from</span>
            <input
              type="time"
              aria-label="Active hours start"
              value={activeHours.start ?? ''}
              onChange={(e) => setStart(e.target.value)}
              className="text-sm rounded border border-edge bg-canvas px-2 py-1 min-h-[44px]"
            />
          </label>
          <label className="flex items-center gap-1 text-sm">
            <span className="text-ink-muted">to</span>
            <input
              type="time"
              aria-label="Active hours end"
              value={activeHours.end ?? ''}
              onChange={(e) => setEnd(e.target.value)}
              className="text-sm rounded border border-edge bg-canvas px-2 py-1 min-h-[44px]"
            />
          </label>
        </div>
        <div className="flex gap-1 mt-2 flex-wrap" role="group" aria-label="Active days">
          {DAY_LABELS.map((label, idx) => {
            const on = days.includes(idx)
            return (
              <button
                key={label}
                type="button"
                aria-pressed={on}
                aria-label={label}
                onClick={() => toggleDay(idx)}
                className={cn(
                  'text-xs rounded border px-2 min-h-[44px] min-w-[44px] transition-colors',
                  on
                    ? 'bg-accent text-accent-ink border-accent'
                    : 'bg-canvas text-ink border-edge hover:bg-accent-wash',
                )}
              >
                {label}
              </button>
            )
          })}
        </div>
        <p className="text-xs text-ink-muted mt-0.5">No days selected = every day.</p>
      </div>

      {/* Raw cron */}
      <div>
        <label className="block text-sm font-medium mb-1" htmlFor="schedule-cron">
          Cron (advanced)
        </label>
        <input
          id="schedule-cron"
          type="text"
          aria-label="Cron expression"
          placeholder="min hour dom mon dow"
          value={cron}
          onChange={(e) => setCronValue(e.target.value)}
          className="text-sm rounded border border-edge bg-canvas px-2 py-1 w-full min-h-[44px] font-mono"
        />
        <p className="text-xs text-ink-muted mt-0.5">
          Fields: minute · hour · day-of-month · month · day-of-week
        </p>
      </div>

      {/* Live preview */}
      <div className="rounded border border-edge bg-surface-raised/30 p-2 text-sm" aria-live="polite">
        {previewError ? (
          <p className="text-red-600 dark:text-red-400" role="alert">{previewError}</p>
        ) : hasCron ? (
          nextRuns.length > 0 ? (
            <div>
              <span className="text-xs font-medium text-ink-muted">Next runs</span>
              <ul className="mt-1 space-y-0.5">
                {nextRuns.map((iso) => (
                  <li key={iso} className="tabular-nums">
                    {(() => {
                      const d = new Date(iso)
                      return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
                    })()}
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            <p className="text-ink-muted">{previewing ? 'Computing next runs…' : 'No upcoming runs.'}</p>
          )
        ) : (
          <p className="text-ink-muted">Active window only — no discrete fire times.</p>
        )}
      </div>

      <div className="flex justify-end">
        <Button
          type="button"
          size="sm"
          onClick={handleSave}
          disabled={saving || invalid}
          className="min-h-[44px]"
        >
          {saving ? 'Saving…' : 'Save schedule'}
        </Button>
      </div>
    </section>
  )
}
