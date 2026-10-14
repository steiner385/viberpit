/**
 * HooksSection — editor for Claude Code hooks (settings.json `hooks`),
 * inline in the Settings tab. Hooks run arbitrary shell on the host, so the
 * section leads with a prominent warning and offers a sandboxed dry-run.
 *
 * react-query is used inline here (not via the shared api/hooks.ts) because
 * this surface is self-contained: GET/PUT `/hooks` and POST `/hooks/dry-run`.
 */
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { cn } from '@/lib/utils'
import { Button } from '@/ui/Button'
import { useConfirm } from '@/ui/useConfirm'
import { api, ApiRequestError } from '@/api/client'

/** A single hook row, matching the backend `HookRow` shape. */
interface HookRow {
  event: string
  matcher: string | null
  command: string
  enabled: boolean
}

interface HooksResponse {
  events: HookRow[]
  warning: string
}

interface DryRunResult {
  stdout: string
  stderr: string
  exitCode: number
  timedOut: boolean
  command: string
}

/** Valid Claude Code hook event names. */
const HOOK_EVENTS = [
  'PreToolUse',
  'PostToolUse',
  'UserPromptSubmit',
  'Notification',
  'Stop',
  'SubagentStop',
  'SessionStart',
  'SessionEnd',
  'PreCompact',
] as const

const inputClass = cn(
  'w-full rounded-md border border-edge-strong',
  'bg-surface text-ink',
  'px-3 py-2 text-sm min-h-target',
  'focus:outline-none focus:ring-2 focus:ring-accent',
)

function errorMessage(err: unknown): string {
  if (err instanceof ApiRequestError) {
    const detail = err.body?.detail
    if (typeof detail === 'string') return detail
  }
  if (err instanceof Error) return err.message
  return 'Something went wrong'
}

export function HooksSection() {
  const qc = useQueryClient()
  const { data, isLoading } = useQuery<HooksResponse>({
    queryKey: ['hooks'],
    queryFn: () => api.get<HooksResponse>('/hooks'),
  })

  const rows = data?.events ?? []

  const saveMutation = useMutation<HooksResponse, unknown, HookRow[]>({
    mutationFn: (events) => api.put<HooksResponse>('/hooks', { events }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ['hooks'] }) },
  })

  // Add-hook form state (kept even after a failed PUT so input isn't lost).
  const [newEvent, setNewEvent] = useState<string>(HOOK_EVENTS[0])
  const [newMatcher, setNewMatcher] = useState('')
  const [newCommand, setNewCommand] = useState('')
  const [addError, setAddError] = useState<string | null>(null)

  // Dry-run state: which row's result is showing, and the result itself.
  const [dryRunFor, setDryRunFor] = useState<number | null>(null)
  const dryRun = useMutation<DryRunResult, unknown, string>({
    mutationFn: (command) => api.post<DryRunResult>('/hooks/dry-run', { command }),
  })

  function saveRows(events: HookRow[], onOk?: () => void) {
    saveMutation.mutate(events, { onSuccess: () => onOk?.() })
  }

  function handleAdd() {
    setAddError(null)
    if (!newCommand.trim()) {
      setAddError('Command is required')
      return
    }
    const row: HookRow = {
      event: newEvent,
      matcher: newMatcher.trim() ? newMatcher.trim() : null,
      command: newCommand,
      enabled: true,
    }
    saveMutation.mutate([...rows, row], {
      onSuccess: () => {
        setNewMatcher('')
        setNewCommand('')
        setAddError(null)
      },
      onError: (err) => setAddError(errorMessage(err)),
    })
  }

  function handleToggle(index: number) {
    const next = rows.map((r, i) => (i === index ? { ...r, enabled: !r.enabled } : r))
    saveRows(next)
  }

  const { confirm, dialog } = useConfirm()
  // Deleting a hook PUTs the whole list; a mis-tap used to take effect for every
  // session with no question asked (2026-09-30 Settings critique, P1).
  async function handleDelete(index: number) {
    const row = rows[index]
    if (!row) return
    const ok = await confirm({
      title: `Delete this ${row.event} hook?`,
      description: `${row.matcher ? `Matcher ${row.matcher} · ` : ''}${row.command}. It stops running for every session on this host.`,
      confirmLabel: 'Delete hook',
    })
    if (!ok) return
    const next = rows.filter((_, i) => i !== index)
    if (dryRunFor === index) setDryRunFor(null)
    saveRows(next)
  }

  function handleDryRun(index: number, command: string) {
    setDryRunFor(index)
    dryRun.mutate(command)
  }

  // Group rows by event, preserving the order events first appear.
  const grouped: { event: string; items: { row: HookRow; index: number }[] }[] = []
  rows.forEach((row, index) => {
    let group = grouped.find((g) => g.event === row.event)
    if (!group) {
      group = { event: row.event, items: [] }
      grouped.push(group)
    }
    group.items.push({ row, index })
  })

  return (
    <div className="space-y-4">
      {dialog}
      {/* Danger banner */}
      <div
        role="alert"
        className={cn(
          'rounded-md border border-warning',
          'bg-warning-wash px-4 py-3',
          'text-sm text-warning',
        )}
      >
        <span className="font-semibold">⚠ Hooks run arbitrary shell commands on this host.</span>{' '}
        {data?.warning ??
          'Only add commands you fully trust — they execute with your user privileges whenever the matching event fires.'}
      </div>

      {/* Save error not tied to the add form (toggle/delete failures) */}
      {saveMutation.isError && !addError && (
        <p role="alert" className="text-sm text-danger">
          {errorMessage(saveMutation.error)}
        </p>
      )}

      {/* Existing hooks */}
      {isLoading ? (
        <p className="text-sm text-ink-muted">Loading hooks…</p>
      ) : rows.length === 0 ? (
        <p className="text-sm text-ink-muted">
          No hooks configured yet. Add one below.
        </p>
      ) : (
        <div className="space-y-4">
          {grouped.map((group) => (
            <div key={group.event} className="space-y-2">
              <h3 className="text-sm font-semibold text-ink">
                {group.event}
              </h3>
              <ul className="space-y-2">
                {group.items.map(({ row, index }) => (
                  <li
                    key={index}
                    className={cn(
                      'rounded-md border border-edge',
                      'bg-surface px-3 py-2 space-y-2',
                    )}
                  >
                    <div className="flex items-start gap-3">
                      <input
                        type="checkbox"
                        checked={row.enabled}
                        onChange={() => handleToggle(index)}
                        disabled={saveMutation.isPending}
                        aria-label={`Enable ${row.event} hook`}
                        className={cn(
                          'mt-1 h-4 w-4 shrink-0 rounded border-edge-strong',
                          'text-accent focus:ring-2 focus:ring-accent',
                        )}
                      />
                      <div className="min-w-0 flex-1 space-y-1">
                        <div className="text-xs text-ink-muted">
                          Matcher: <span className="font-mono">{row.matcher ?? '(all)'}</span>
                        </div>
                        <code className="block break-all font-mono text-sm text-ink">
                          {row.command}
                        </code>
                      </div>
                      <div className="flex shrink-0 items-center gap-2">
                        <Button
                          type="button"
                          variant="secondary"
                          size="sm"
                          onClick={() => handleDryRun(index, row.command)}
                          disabled={dryRun.isPending && dryRunFor === index}
                        >
                          Dry run
                        </Button>
                        <Button
                          type="button"
                          variant="danger"
                          size="sm"
                          onClick={() => void handleDelete(index)}
                          disabled={saveMutation.isPending}
                        >
                          Delete
                        </Button>
                      </div>
                    </div>

                    {dryRunFor === index && (
                      <DryRunPanel result={dryRun.data} isPending={dryRun.isPending} error={dryRun.error} />
                    )}
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}

      {/* Add hook form */}
      <div className="space-y-3 border-t border-edge pt-4">
        <h3 className="text-sm font-semibold text-ink">Add hook</h3>

        <div className="space-y-1">
          <label
            htmlFor="hooks-new-event"
            className="block text-sm font-medium text-ink"
          >
            Event
          </label>
          <select
            id="hooks-new-event"
            value={newEvent}
            onChange={(e) => setNewEvent(e.target.value)}
            className={inputClass}
          >
            {HOOK_EVENTS.map((ev) => (
              <option key={ev} value={ev}>{ev}</option>
            ))}
          </select>
        </div>

        <div className="space-y-1">
          <label
            htmlFor="hooks-new-matcher"
            className="block text-sm font-medium text-ink"
          >
            Matcher <span className="text-ink-muted">(optional)</span>
          </label>
          <input
            id="hooks-new-matcher"
            type="text"
            value={newMatcher}
            onChange={(e) => setNewMatcher(e.target.value)}
            placeholder="e.g. Bash — leave blank for all"
            className={inputClass}
          />
        </div>

        <div className="space-y-1">
          <label
            htmlFor="hooks-new-command"
            className="block text-sm font-medium text-ink"
          >
            Command
          </label>
          <input
            id="hooks-new-command"
            type="text"
            value={newCommand}
            onChange={(e) => setNewCommand(e.target.value)}
            placeholder="e.g. jq -r .tool_name >> ~/hook.log"
            className={inputClass}
          />
        </div>

        {addError && (
          <p role="alert" className="text-sm text-danger">
            {addError}
          </p>
        )}

        <div className="flex justify-end">
          <Button
            type="button"
            onClick={handleAdd}
            disabled={saveMutation.isPending}
            className="min-h-target"
          >
            {saveMutation.isPending ? 'Adding…' : 'Add'}
          </Button>
        </div>
      </div>
    </div>
  )
}

function DryRunPanel({
  result,
  isPending,
  error,
}: {
  result: DryRunResult | undefined
  isPending: boolean
  error: unknown
}) {
  return (
    <div className="rounded-md border border-edge bg-canvas p-3 text-xs">
      {isPending ? (
        <p className="text-ink-muted">Running…</p>
      ) : error ? (
        <p role="alert" className="text-danger">{errorMessage(error)}</p>
      ) : result ? (
        <div className="space-y-2">
          <div className="flex items-center gap-3">
            <span className="font-semibold text-ink">
              Exit code: <span className="font-mono">{result.exitCode}</span>
            </span>
            {result.timedOut && (
              <span className="text-warning">timed out</span>
            )}
          </div>
          {result.stdout && (
            <div>
              <div className="text-ink-muted">stdout</div>
              <pre className="whitespace-pre-wrap break-all font-mono text-ink">
                {result.stdout}
              </pre>
            </div>
          )}
          {result.stderr && (
            <div>
              <div className="text-ink-muted">stderr</div>
              <pre className="whitespace-pre-wrap break-all font-mono text-danger">
                {result.stderr}
              </pre>
            </div>
          )}
        </div>
      ) : null}
    </div>
  )
}
