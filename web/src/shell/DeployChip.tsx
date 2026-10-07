import { LoaderCircle, Rocket, TriangleAlert } from 'lucide-react'
import { useDeployState } from '@/lib/deployState'
import { ciLabel } from '@/lib/ciVerdict'
import { cn } from '@/lib/utils'
import { settingsSectionPath } from '@/shell/navDefs'
import { NavItem } from './regions/NavItem'

/** Rail summary of Settings › Deploy's shared readiness state; never starts a deploy. */
export function DeployChip({ showLabel }: { showLabel: boolean }) {
  const s = useDeployState()
  if (!s.behind && !s.running && !s.hasFailure) return null

  // The same full-tip / green-frontier distinction displayed by the deploy page.
  // A green tip covers every change, including older API responses without a count.
  const ready = s.partial ? s.deployableCount : s.ci === 'green' ? s.count : 0
  const fraction = `${ready}/${s.count}`
  const warning = s.hasFailure || s.tipCi === 'red'
  const status = s.running ? 'Deploying…' : s.hasFailure ? 'Needs attention' : `CI ${ciLabel(s.tipCi)}`
  const summary = [
    'Deploy:',
    `${s.count} change${s.count === 1 ? '' : 's'} pending deploy.`,
    s.running ? 'Deployment in progress.' : `${ready} deployable now.`,
    `Latest CI ${ciLabel(s.tipCi)}.`,
    s.hasFailure ? 'Needs attention.' : '',
  ].filter(Boolean).join(' ')
  const Icon = s.running ? LoaderCircle : Rocket

  return (
    <NavItem
      to={settingsSectionPath('deploy')}
      icon={Icon}
      label={summary}
      showLabel={showLabel}
      summaryTooltip
      className={cn(
        'py-1',
        s.canDeploy && 'bg-accent-wash text-accent hover:bg-accent-wash hover:text-accent',
      )}
      visualContent={
        <span className={cn('flex min-w-0 items-center', showLabel ? 'w-full gap-2' : 'flex-col gap-0.5')}>
          <span className="relative shrink-0">
            <Icon size={20} className={s.running ? 'motion-safe:animate-spin' : undefined} />
            {warning && !showLabel && <TriangleAlert size={12} className="absolute -right-2 -top-1 bg-surface text-warning" />}
          </span>
          {showLabel ? (
            <span className="min-w-0 text-left">
              <span className="block truncate tabular-nums">{s.count > 0 ? `Deploy · ${fraction} ready` : 'Deploy'}</span>
              <span className={cn('flex items-center gap-1 text-xs', warning && 'text-warning')}>
                {warning && <TriangleAlert size={12} className="shrink-0" />}
                {status}
              </span>
            </span>
          ) : s.count > 0 ? (
            <span className="max-w-full break-all text-center text-2xs font-bold leading-tight tabular-nums">{fraction}</span>
          ) : null}
        </span>
      }
    />
  )
}
