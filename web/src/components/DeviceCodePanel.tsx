/**
 * The device-code half of an enrolment, for every provider that uses one.
 *
 * GitHub Copilot and OpenAI/ChatGPT run the identical state machine — the
 * backend spells the two payloads identically on purpose — and OpenAI reauth
 * runs it a third time, on the per-account routes. Three copies of this markup
 * is exactly the parallel-implementation shape this repo bans: the first
 * divergence would be a provider whose "Authorization failed" line never
 * appeared, or a code that stayed on screen after the flow moved on.
 *
 * ## Security
 *
 * `userCode` and `verificationUri` are the only login details the authority is
 * allowed to emit, and they are single-use and short-lived. This component
 * RENDERS them and does nothing else with them: no localStorage, no query-cache
 * persistence, no analytics call, no error/telemetry payload. Keeping them
 * inside one render path is what makes that auditable — see
 * `DeviceCodePanel.test.tsx`, which fails if anything writes one to storage.
 */
import { cn } from '@/lib/utils'
import type { DeviceCodeState } from '@/api/types'

export interface DeviceCodePanelProps {
  /** The polled state; undefined before the flow starts (or after a cancel). */
  state?: DeviceCodeState
  /** Whether the user has actually started this flow — a `pending` state is only
   *  worth narrating once they have. */
  started: boolean
  /** Where to go, e.g. "GitHub" / "ChatGPT". Used in the instruction line only. */
  providerLabel: string
}

export function DeviceCodePanel({ state, started, providerLabel }: DeviceCodePanelProps) {
  if (state?.state === 'awaiting_authorization' && state.userCode) {
    return (
      <div className="mt-4 space-y-2 rounded-lg border border-amber-200 dark:border-amber-700 bg-amber-50 dark:bg-amber-900/20 p-3">
        <p className="text-xs text-amber-700 dark:text-amber-300">
          Enter this code at the link below to authorize with {providerLabel}:
        </p>
        <p className="text-lg font-mono font-bold tracking-widest text-amber-900 dark:text-amber-100 text-center">
          {state.userCode}
        </p>
        {state.verificationUri && (
          <a
            href={state.verificationUri}
            target="_blank"
            rel="noopener noreferrer"
            className={cn(
              'block text-xs text-accent hover:underline text-center',
              // A device code is typed on a phone as often as clicked, so the
              // link is a 44px target rather than a bare line of text.
              'min-h-[44px] flex items-center justify-center',
            )}
          >
            {state.verificationUri}
          </a>
        )}
      </div>
    )
  }

  if (started && (state === undefined || state.state === 'pending')) {
    return (
      <p className="mt-4 text-xs text-amber-600 dark:text-amber-400">
        Starting device authorization…
      </p>
    )
  }

  if (state?.state === 'failed') {
    return (
      <p className="mt-4 text-xs text-danger" role="alert">
        {/* The server has already scrubbed this string; showing it beats a fixed
            line that cannot say the account was already enrolled. */}
        {state.error || 'Authorization failed. Please try again.'}
      </p>
    )
  }

  return null
}
