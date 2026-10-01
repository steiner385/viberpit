import { Rocket } from 'lucide-react'
import { useDeployState } from '@/lib/deployState'
import { settingsSectionPath } from '@/shell/navDefs'
import { NavItem } from './regions/NavItem'

/**
 * "N changes pending deploy", at the foot of the rail.
 *
 * This is where the pending-deploy banner went (2026-10-01 shell critique,
 * P2). As a banner it was 124px of information-with-a-button at the top of
 * every desktop page, above the needs-you states the operator came for, and
 * the first two Tab stops on every non-Sessions route. Pending commits are
 * inventory: master advances all day, and a notice that is always there is
 * not a signal. So it is a quiet rail item — present only while something is
 * pending, gone when the server is current — that opens Settings › Deploy,
 * the full surface (CI verdict, green-frontier target, the commit list and
 * Deploy now, behind its confirm). Failure stays a banner (`DeployPanel`).
 *
 * A `NavItem`, so it is the rail's 44px row with the rail's focus ring, and
 * its accessible name carries the count whether or not the label is shown.
 * No count pill: the rail's one badge rule (`useNavBadges`) is "an amber
 * needs-you count or nothing", and this is neither — its presence is the
 * signal, its label the number. It is not `active`: Settings is the section,
 * and the one `aria-current` on the rail is the Settings item's.
 *
 * Rail only. The phone hides the banner already and points at Settings ›
 * Deploy with the Settings badge; the kiosk has no rail.
 */
export function DeployChip({ showLabel }: { showLabel: boolean }) {
  const s = useDeployState()
  if (!s.behind) return null
  return (
    <NavItem
      to={settingsSectionPath('deploy')}
      icon={Rocket}
      label={`${s.count} change${s.count === 1 ? '' : 's'} pending deploy`}
      showLabel={showLabel}
    />
  )
}
