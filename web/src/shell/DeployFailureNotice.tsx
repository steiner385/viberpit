// web/src/components/DeployFailureNotice.tsx
import type { DeployState } from '@/lib/deployState'

/**
 * What went wrong with the last deploy, in the exact words both surfaces use.
 *
 * Shared by the banner (`DeployPanel`) and Settings › Deploy. The distinction it
 * draws — `failed` is autopilot still retrying, `gaveUp` needs a human — is the
 * whole point of the copy, so it is written once. The caller owns the container
 * (the banner puts it under a rule; the section puts it in a danger group).
 */
export function DeployFailureNotice({ state }: { state: DeployState }) {
  const { showError, error, failed, gaveUp } = state
  return (
    <>
      {showError && <p className="text-xs text-danger">{error}</p>}
      {failed.length > 0 && (
        <p className="mt-1 text-xs text-danger">
          {failed.length} session{failed.length === 1 ? '' : 's'} failed to resume — autopilot is
          recovering {failed.length === 1 ? 'it' : 'them'}…{' '}
          <span className="font-mono">{failed.join(', ')}</span>
        </p>
      )}
      {gaveUp.length > 0 && (
        <p className="mt-1 text-xs text-danger">
          {gaveUp.length} session{gaveUp.length === 1 ? '' : 's'} couldn’t be recovered — open to
          retry or archive:{' '}
          <span className="font-mono">{gaveUp.join(', ')}</span>
        </p>
      )}
    </>
  )
}
