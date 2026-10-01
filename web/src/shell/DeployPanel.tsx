// web/src/shell/DeployPanel.tsx
import { useState } from 'react'
import { Banner } from '@/ui/Banner'
import { useDeployState } from '@/lib/deployState'
import { DeployFailureNotice } from './DeployFailureNotice'
import { storageKey, migrateStorageNamespace } from '@/lib/storageKey'

// The failure notice is dismissed by CONTENT, not as a boolean: `gaveUp` is the daemon's
// live set and only changes when a human retries/archives, so a bare "hidden" flag would
// either mask the next failure or (if session-scoped) reappear on every reload — the
// original complaint. Persisting the signature of what was dismissed means the same
// failure stays gone across reloads while any new session id or error text brings the
// notice back on its own. Guarded like `lib/lastRepo.ts`: a blocked localStorage degrades
// to "not dismissed", never a crash.
const DISMISSED_KEY = storageKey('deployFailureDismissed')

function failureSignature(error: string | null, failed: string[], gaveUp: string[]): string {
  return JSON.stringify({ error, failed: [...failed].sort(), gaveUp: [...gaveUp].sort() })
}

function readDismissedSignature(): string | null {
  try {
    return window.localStorage.getItem(
      migrateStorageNamespace(window.localStorage, DISMISSED_KEY))
  } catch {
    return null
  }
}

function writeDismissedSignature(sig: string): void {
  try {
    window.localStorage.setItem(DISMISSED_KEY, sig)
  } catch {
    /* ignore — persistence is best-effort */
  }
}

/**
 * A deploy FAILURE as a banner, for the app-level stack — and only a failure.
 *
 * Pending commits used to live here too, with a Deploy button and a commit
 * list: 124px of information-with-a-button at the top of every desktop page,
 * above the needs-you states the operator actually came for, and the first
 * two Tab stops on every non-Sessions route (2026-10-01 shell critique, P2).
 * "N changes pending" is inventory, not an interruption. It is now the
 * `DeployChip` at the rail's foot, which opens Settings › Deploy — the full
 * surface, with the CI verdict, the green-frontier target and Deploy now.
 * The stack keeps what genuinely needs a look: a failed deploy, or sessions
 * that did not come back.
 *
 * Hidden on phone (`hideOn: ['phone']` in `shell/useAppBanners`); Settings ›
 * Deploy is the phone's only deploy surface and the Settings nav badge points
 * at it. The state itself comes from `useDeployState`, shared with that section.
 */
export function DeployPanel() {
  const s = useDeployState()

  const [dismissedSig, setDismissedSig] = useState(readDismissedSignature)
  const failureSig = failureSignature(s.showError ? s.error : null, s.failed, s.gaveUp)
  const showFailure = s.hasFailure && dismissedSig !== failureSig
  const dismissFailure = () => {
    writeDismissedSignature(failureSig)
    setDismissedSig(failureSig)
  }

  if (!showFailure) return null

  return (
    <Banner
      tone="warning"
      // Not a live region: phase state and error text change while a deploy
      // runs, and role="status" would announce every one of those to a screen reader.
      role="none"
      onDismiss={dismissFailure}
    >
      <DeployFailureNotice state={s} />
    </Banner>
  )
}
