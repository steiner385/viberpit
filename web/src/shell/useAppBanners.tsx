import type { BannerStackItem } from '@/ui/BannerStack'
import { GlobalUpdateBanner } from '@/shell/GlobalUpdateBanner'
import { DaemonRestartBanner } from '@/shell/DaemonRestartBanner'
import { GitCredentialBanner } from '@/shell/GitCredentialBanner'
import { AllLimitedBanner } from '@/shell/AllLimitedBanner'
import { UpdatePendingBanner } from '@/shell/UpdatePendingBanner'
import { DeployPanel } from '@/shell/DeployPanel'
import { useInputHold } from '@/hooks/useInputHold'

export interface AppBannersOptions {
  /** ISO reset time for the all-limited banner, or null/undefined for hidden.
   *  Owned by the shell because both old shells owned a copy. */
  allLimitedReset: string | null | undefined
  onDismissAllLimited: () => void
}

/**
 * The one place app-level banners are declared.
 *
 * Before this, banners were declared in three components (App, Dashboard,
 * DesktopShell) with different sets — which is how `input-hold` came to render
 * on phones only, and how `all-limited` and `deploy` came to be duplicated with
 * two copies of the auto-clear logic.
 *
 * Priorities are carried over unchanged so the visual order does not move.
 * `BannerStack` already sorts by priority descending and drops null nodes, so
 * this hook does not sort.
 */
export function useAppBanners({
  allLimitedReset,
  onDismissAllLimited,
}: AppBannersOptions): BannerStackItem[] {
  const inputHold = useInputHold()

  return [
    { id: 'daemon', priority: 90, tone: 'danger', node: <DaemonRestartBanner /> },
    { id: 'git-credential', priority: 80, tone: 'warning', node: <GitCredentialBanner /> },
    {
      id: 'all-limited', priority: 70, tone: 'danger',
      node: allLimitedReset !== undefined
        ? <AllLimitedBanner earliestReset={allLimitedReset} onDismiss={onDismissAllLimited} />
        : null,
    },
    {
      id: 'input-hold', priority: 60, tone: 'info',
      node: inputHold.holding
        ? <UpdatePendingBanner queuedCount={inputHold.count} mode={inputHold.mode}
            onSendNow={inputHold.sendNow} onDiscard={inputHold.discard} />
        : null,
    },
    // A deploy FAILURE only (2026-10-01 shell critique, P2): pending commits
    // moved to the rail-foot `DeployChip`. `hideOn: ['phone']` stays — the
    // phone has no rail, and Settings › Deploy is its one deploy surface, with
    // the Settings nav badge pointing at it.
    { id: 'deploy', priority: 50, tone: 'warning', hideOn: ['phone'], node: <DeployPanel /> },
    { id: 'update', priority: 30, tone: 'info', node: <GlobalUpdateBanner /> },
  ]
}
