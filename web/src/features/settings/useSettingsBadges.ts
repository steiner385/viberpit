import { useAccounts } from '@/api/hooks'
import type { SettingsBadgeKey } from './settingsDefs'

/** In-Settings badges (C20): a COUNT inside Settings; the rail keeps its dot. Same predicate as useNavBadges. */
export function useSettingsBadges(): Record<SettingsBadgeKey, number | undefined> {
  const { data: accounts = [] } = useAccounts()
  const n = accounts.filter((a) => a.status === 'needs-reauth' || a.status === 'needs-relogin').length
  return { accountsAttention: n || undefined }
}
