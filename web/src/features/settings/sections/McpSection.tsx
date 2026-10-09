import { useState } from 'react'
import { useLibraryMcp, useTestMcp } from '@/api/hooks'
import { apiErrorCode } from '@/api/client'
import type { LibraryMcpServer } from '@/api/types'
import { McpServerDialog } from './McpServerDialog'
import { McpAuthFlow } from './McpAuthFlow'
import { McpStatusPill } from './McpStatusPill'
import { McpScopeControl } from './McpScopeControl'
import { relativeAge, serverKey, worstStatus } from './mcpStatus'

// Accounts whose latest status row for this server is needs-auth (de-duplicated).
function needsAuthAccounts(s: LibraryMcpServer): string[] {
  return [...new Set(s.status.filter((r) => r.status === 'needs-auth').map((r) => r.account))]
}

function testErrorMessage(err: unknown): string {
  switch (apiErrorCode(err)) {
    case 'unknown_account':
      return 'Test failed: unknown account.'
    case 'mcp_probe_timeout':
      return 'Test timed out. The host did not answer in time; try again.'
    default:
      return 'Test failed.'
  }
}

interface TestOutcome {
  /** Server name the test targeted, or null for Test all. */
  target: string | null
  skipped: { account: string; reason: string }[]
  error: string | null
}

function TestOutcomeNote({ outcome, target }: { outcome: TestOutcome | null; target: string | null }) {
  if (!outcome || outcome.target !== target) return null
  return (
    <>
      {outcome.error && (
        <p className="text-xs text-danger break-words" role="alert">
          {outcome.error}
        </p>
      )}
      {outcome.skipped.length > 0 && (
        <p className="text-xs text-ink-muted break-words">
          Skipped: {outcome.skipped.map((k) => `${k.account} (${k.reason})`).join(', ')}
        </p>
      )}
    </>
  )
}

// Reauth for one server: McpAuthFlow starts auth for a single account, so with several
// needs-auth accounts a picker chooses which one (default: the first).
function ServerReauth({ server, accounts }: { server: string; accounts: string[] }) {
  const [picked, setPicked] = useState(accounts[0])
  const account = accounts.includes(picked) ? picked : accounts[0]
  return (
    <div>
      {accounts.length > 1 && (
        <label className="mt-1 px-3 flex flex-wrap items-center gap-2 text-xs text-ink-muted">
          Account to reauthorize
          <select
            value={account}
            onChange={(e) => setPicked(e.target.value)}
            className="rounded border border-edge-strong bg-surface text-ink px-1 py-0.5 text-xs"
          >
            {accounts.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
        </label>
      )}
      <McpAuthFlow key={account} server={server} accounts={[account]} />
    </div>
  )
}

export function McpSection() {
  const { data, isLoading } = useLibraryMcp()
  const test = useTestMcp()
  const [dialogServer, setDialogServer] = useState<LibraryMcpServer | null | undefined>(undefined)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [outcome, setOutcome] = useState<TestOutcome | null>(null)

  if (isLoading) return <div className="text-sm opacity-70 p-4">Loading…</div>

  const servers = data?.servers ?? []

  function runTest(target: string | null) {
    setOutcome(null)
    test.mutate(target == null ? {} : { server: target }, {
      onSuccess: (res) => setOutcome({ target, skipped: res.skipped, error: null }),
      onError: (err) => setOutcome({ target, skipped: [], error: testErrorMessage(err) }),
    })
  }

  function toggle(key: string) {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (!next.delete(key)) next.add(key)
      return next
    })
  }

  const testingTarget = test.isPending ? (test.variables?.server ?? null) : undefined

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <p className="text-xs opacity-60 flex-1 min-w-[12rem]">
          MCP servers are shared across accounts; changes apply to newly started sessions. Test
          checks each server without starting a session.
        </p>
        <button
          type="button"
          onClick={() => runTest(null)}
          disabled={test.isPending}
          className="rounded border border-edge-strong px-2 py-1 text-xs hover:bg-surface-raised disabled:opacity-40"
        >
          {testingTarget === null ? 'Testing…' : 'Test all'}
        </button>
      </div>
      <TestOutcomeNote outcome={outcome} target={null} />
      <ul className="space-y-2">
        {servers.map((s) => {
          const key = `${s.source}/${s.name}`
          const needsAuth = needsAuthAccounts(s)
          const open = expanded.has(key)
          return (
          <li key={key}>
            <button
              className="w-full text-left border border-edge rounded-lg p-3 hover:bg-surface-raised focus:outline-none focus:ring-2 focus:ring-accent"
              aria-label={s.name}
              onClick={() => setDialogServer(s)}
            >
              <div className="flex items-center gap-2 flex-wrap">
                <span className="font-medium break-all">{s.name}</span>
                {/* Transport badge */}
                <span className="text-xs px-1.5 py-0.5 rounded-full border border-edge opacity-70">
                  {s.transport}
                </span>
                {/* Source badge (plugin id only — omit for 'user') */}
                {s.source !== 'user' && (
                  <span className="text-xs px-1.5 py-0.5 rounded-full border border-edge opacity-70">
                    {s.source}
                  </span>
                )}
                <McpStatusPill status={worstStatus(s.status.map((r) => r.status))} />
              </div>
            </button>
            {/* Siblings below the card (never nested inside the card button). */}
            <div className="mt-1 px-3 flex flex-wrap items-center gap-2">
              {s.status.length > 0 && (
                <button
                  type="button"
                  aria-label={`Details for ${s.name}`}
                  aria-expanded={open}
                  aria-controls={`mcp-details-${key}`}
                  onClick={() => toggle(key)}
                  className="rounded border border-edge px-2 py-1 text-xs hover:bg-surface-raised"
                >
                  {open ? 'Hide details' : 'Details'}
                </button>
              )}
              <button
                type="button"
                aria-label={`Test ${s.name}`}
                disabled={test.isPending}
                onClick={() => runTest(s.name)}
                className="rounded border border-edge px-2 py-1 text-xs hover:bg-surface-raised disabled:opacity-40"
              >
                {testingTarget === s.name ? 'Testing…' : 'Test'}
              </button>
            </div>
            <McpScopeControl serverKey={serverKey(s)} />
            <div className="px-3">
              <TestOutcomeNote outcome={outcome} target={s.name} />
            </div>
            {open && (
              <ul id={`mcp-details-${key}`} className="mt-1 px-3 space-y-2">
                {s.status.map((r) => (
                  <li
                    key={`${r.account}/${r.host}`}
                    className="border-l-2 border-edge pl-2 text-xs"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span>
                        {r.account} · {r.host}
                      </span>
                      <McpStatusPill status={r.status} />
                    </div>
                    {r.error && <p className="text-ink-muted break-words">{r.error}</p>}
                    <p className="text-ink-muted break-words">
                      checked {relativeAge(r.observedAt)} · from {r.source}
                    </p>
                  </li>
                ))}
              </ul>
            )}
            {/* Inline auth flow — sibling below the card button to avoid nesting interactive elements */}
            {needsAuth.length > 0 && (s.transport === 'http' || s.transport === 'sse') && (
              <ServerReauth server={s.name} accounts={needsAuth} />
            )}
            {needsAuth.length > 0 && s.transport === 'stdio' && (
              <p className="mt-1 px-3 pb-2 text-sm text-ink-muted">
                This server cannot use browser OAuth here. Configure credentials on the host
                running the session using the server’s setup instructions, then start a new session.
              </p>
            )}
          </li>
          )
        })}
        {servers.length === 0 && (
          <li className="text-sm opacity-70 p-4">No MCP servers configured.</li>
        )}
      </ul>

      <div className="mt-4">
        <button
          type="button"
          onClick={() => setDialogServer(null)}
          className="rounded border border-edge-strong px-3 py-2 text-sm hover:bg-surface-raised"
        >
          Add server
        </button>
      </div>

      {/* dialogServer === undefined means no dialog open */}
      {dialogServer !== undefined && (
        <McpServerDialog
          server={dialogServer}
          onClose={() => setDialogServer(undefined)}
        />
      )}
    </div>
  )
}
