/**
 * The sandbox hold (D-23), rendered by `SessionErrorBanner` in its own chrome (it hands
 * down the class). Lazy: loaded only while a session is held because its sandbox failed.
 * States the reason, and offers two actions:
 *
 * - **Retry** — `POST /sandbox {enabled: true}`: respawn sandboxed and verify again.
 *   Hidden while the host's facts say the capability is off (the kill switch): the
 *   server would refuse it (`SANDBOX_DISABLED`), and turning off is the only way on.
 * - **Turn off sandbox and continue** — `POST /sandbox {enabled: false}`, behind the
 *   shared `SandboxConfirmDialog`: running uncontained is an explicit human decision.
 *
 * Either respawns the session; a refusal, or a Retry whose sandbox fails again, is
 * stated inline. The hold itself clears when the session payload drops it.
 */
import { useState } from 'react'
import { useSandboxFacts, useSetSandbox } from '@/api/sandboxHooks'
import { Button } from '@/ui/Button'
import { SandboxConfirmDialog } from '../SandboxConfirmDialog'
import { sandboxError } from '../sandboxCopy'

/** The house 44px floor on a coarse pointer, compact under a mouse. */
const ACTION = 'h-auto min-h-11 [@media(hover:hover)]:min-h-0 px-2 py-1 text-xs'

export default function SandboxHoldBanner({ sessionId, reason, className }: {
  sessionId: string
  reason: string | null
  className?: string
}) {
  const toggle = useSetSandbox()
  // The card's own query (shared cache). Retry waits for the facts and hides only when
  // they say the capability is off; a failed read offers it, and the server decides.
  const facts = useSandboxFacts(sessionId)
  const canRetry = facts.isError || (facts.data !== undefined && facts.data.capability !== false)
  const [confirmOff, setConfirmOff] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState<'retry' | 'off' | null>(null)

  async function apply(enabled: boolean) {
    setError(null)
    setPending(enabled ? 'retry' : 'off')
    try {
      const res = await toggle.mutateAsync({ id: sessionId, enabled })
      if (res.held) setError(`Still held: ${res.reason || 'the sandbox did not start'}`)
    } catch (err) {
      setError(sandboxError(err))
    } finally {
      setPending(null)
      setConfirmOff(false)
    }
  }

  return (
    <div role="alert" className={className} data-testid="sandbox-hold">
      <div className="flex-1 min-w-0 basis-60 break-words">
        <span className="font-medium">Sandbox failed — session held: </span>
        {reason || 'the sandbox did not start'}
        <span className="block text-xs text-ink-muted">Nothing runs until you retry, or turn the sandbox off.</span>
      </div>
      <div className="flex flex-wrap items-center justify-end gap-2">
        {canRetry && (
          <Button
            variant="secondary"
            size="sm"
            className={ACTION}
            loading={pending === 'retry'}
            disabled={toggle.isPending}
            onClick={() => void apply(true)}
          >
            Retry
          </Button>
        )}
        <Button
          variant="ghost"
          size="sm"
          className={`${ACTION} text-danger`}
          disabled={toggle.isPending}
          onClick={() => { setError(null); setConfirmOff(true) }}
        >
          Turn off sandbox and continue
        </Button>
        {/* Inside the hold's own alert region, so it is announced without a nested alert. */}
        {error && <p className="w-full text-right text-xs" data-testid="sandbox-hold-error">{error}</p>}
        <SandboxConfirmDialog
          open={confirmOff}
          enabled={false}
          held
          busy={pending === 'off'}
          onClose={() => setConfirmOff(false)}
          onConfirm={() => apply(false)}
        />
      </div>
    </div>
  )
}
