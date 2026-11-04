import { useEffect, useState } from 'react'
import { toast } from 'sonner'
import { useConfig, useLibraryMcp, useTestMcp } from '@/api/hooks'
import { apiErrorCode } from '@/api/client'
import type { LibraryMcpServer } from '@/api/types'
import { McpServerDialog } from './McpServerDialog'
import { McpAuthFlow } from './McpAuthFlow'
import { McpStatusPill } from './McpStatusPill'
import { McpScopeControl } from './McpScopeControl'
import { serverKey } from './mcpServerKey'
import { relativeAge } from './mcpStatus'

// Grants for which signing in again is the fix.
const SIGN_IN_GRANTS = ['never', 'dead', 'expired']

// Fixed copy per closed sign-in error code; the raw query text is never rendered.
const SIGN_IN_ERRORS: Record<string, string> = {
  state_invalid: 'That sign-in link expired or was already used. Try again.',
  denied: 'Sign-in was cancelled at the provider.',
  exchange_failed: "The provider didn't accept the sign-in. Try again.",
  registration_failed: "This provider doesn't allow ViberPit to register. Use the manual sign-in.",
  not_configured: "That server can't be signed in here.",
}

function signInErrorMessage(code: string): string {
  return Object.hasOwn(SIGN_IN_ERRORS, code) ? SIGN_IN_ERRORS[code] : 'Sign-in failed.'
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

export function McpSection() {
  const { data, isLoading } = useLibraryMcp()
  const test = useTestMcp()
  const { data: config } = useConfig()
  const oneTap = config?.oneTapEnabled === true
  // The one-tap redirect lands here with ?pasteBack / ?signedIn / ?signInError.
  const [pasteBack] = useState(() => new URLSearchParams(window.location.search).get('pasteBack'))
  const [dialogServer, setDialogServer] = useState<LibraryMcpServer | null | undefined>(undefined)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [outcome, setOutcome] = useState<TestOutcome | null>(null)

  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const signedIn = params.get('signedIn')
    const signInError = params.get('signInError')
    if (signedIn) toast.success(`Signed in to ${signedIn} — every account and runner can use it now.`)
    if (signInError !== null) toast.error(signInErrorMessage(signInError))
    if (['pasteBack', 'signedIn', 'signInError'].some((k) => params.has(k))) {
      window.history.replaceState(window.history.state, '', window.location.pathname)
    }
  }, [])

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
          const signInNeeded = s.overall.status === 'needs-auth' && SIGN_IN_GRANTS.includes(s.overall.grant)
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
                <McpStatusPill status={s.overall.status} />
              </div>
            </button>
            {s.overall.error && (
              <p className="mt-1 px-3 text-xs text-ink-muted break-words">{s.overall.error}</p>
            )}
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
                    className={`border-l-2 border-edge pl-2 text-xs${r.counted ? '' : ' opacity-60'}`}
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span>
                        {r.account} · {r.host}
                      </span>
                      <McpStatusPill status={r.status} />
                    </div>
                    {r.error && <p className="text-ink-muted break-words">{r.error}</p>}
                    <p className="text-ink-muted break-words">
                      checked {relativeAge(r.observedAt)} · from {r.source}{r.counted ? '' : ' · not counted'}
                    </p>
                  </li>
                ))}
              </ul>
            )}
            {/* Inline auth flow — sibling below the card button to avoid nesting interactive elements */}
            {((signInNeeded && !oneTap) || pasteBack === s.name) && (s.transport === 'http' || s.transport === 'sse') && (
              <>
                {pasteBack === s.name && (
                  <p className="mt-1 px-3 text-xs text-ink-muted">
                    One-tap sign-in isn't available for this server; sign in manually below.
                  </p>
                )}
                <McpAuthFlow server={s.name} accounts={[]} />
              </>
            )}
            {signInNeeded && oneTap && pasteBack !== s.name && (s.transport === 'http' || s.transport === 'sse') && (
              <div className="mt-1 px-3 pb-2">
                <a
                  href={`/api/mcp/oauth/start?server=${encodeURIComponent(s.name)}`}
                  className="inline-block rounded border border-edge-strong px-2 py-1 text-xs hover:bg-surface-raised"
                >
                  Sign in
                </a>
              </div>
            )}
            {s.overall.status === 'needs-auth' && s.transport === 'stdio' && (
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
