import { useAccounts } from '@/api/hooks'
import { useDeployState } from '@/lib/deployState'
import type { SettingsBadgeKey } from './settingsDefs'

/** In-Settings badges (C20): a COUNT inside Settings; the rail keeps its dot. Same predicate as useNavBadges. */
export function useSettingsBadges(): Record<SettingsBadgeKey, number | undefined> {
  const { data: accounts = [] } = useAccounts()
  const n = accounts.filter((a) => a.status === 'needs-reauth' || a.status === 'needs-relogin').length
  // How many commits are waiting. On a phone this badge replaces the deploy
  // banner outright (`hideOn: ['phone']`), so it carries the count the banner's
  // title used to — reaching the section is otherwise a blind two-tap trip.
  const deploy = useDeployState()
  return {
    accountsAttention: n || undefined,
    deployPending: deploy.count || undefined,
  }
}
