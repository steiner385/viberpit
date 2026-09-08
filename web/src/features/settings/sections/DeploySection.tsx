import { Button } from '@/ui/Button'
import { useDeployState } from '@/lib/deployState'
import { useDeployController } from '@/lib/deployController'
import { DeployFailureNotice } from '@/components/DeployFailureNotice'
import { SETTINGS_BY_ID } from '../settingsDefs'
import { SettingsSection } from '../chassis/SettingsSection'
import { SettingsGroup } from '../chassis/SettingsGroup'

/**
 * Deploy, at full size — and on a phone, the ONLY size.
 *
 * The app-level banner (`DeployPanel`) carries this state everywhere except
 * phone, where it is hidden (`hideOn: ['phone']` in `shell/useAppBanners`)
 * because it is the tallest banner in the stack. Both surfaces read
 * `useDeployState`, so the CI verdict, the green-frontier target and the Deploy
 * button's enabled-ness cannot disagree between them.
 *
 * Two deliberate differences from the banner:
 *  - It says something when there is nothing to do. A banner earns its space by
 *    disappearing; a Settings section that renders empty reads as broken.
 *  - The failure notice is NOT dismissible here. This is where you arrive after
 *    dismissing the banner — or, on a phone, the only place it ever appeared —
 *    so a dismiss control would make a gave-up session unreachable.
 */
export function DeploySection() {
  const s = useDeployState()
  const { start, starting } = useDeployController()

  return (
    <SettingsSection id="deploy" title="Deploy" scope="server"
      description={SETTINGS_BY_ID.deploy.description}>
      <SettingsGroup legend="Pending changes">
        {s.behind ? (
          <div className="flex flex-col gap-3 py-3">
            <div className="flex flex-wrap items-center gap-3">
              <span className="text-sm text-ink">
                {s.count} change{s.count === 1 ? '' : 's'} pending deploy
              </span>
              {s.reason && <span className="text-xs text-warning">{s.reason}</span>}
              <Button
                size="sm"
                variant="secondary"
                className="ml-auto"
                disabled={!s.canDeploy || starting}
                onClick={() => start(s.deployTarget)}
              >
                {starting ? 'Starting…' : 'Deploy now'}
              </Button>
            </div>
            {/* Always expanded, unlike the banner's collapsed-by-default list: you
                navigated here to see it, and the page can afford the height. */}
            <ul className="flex flex-col gap-1">
              {s.commits.map((c) => (
                <li key={c.sha} className="flex gap-2 font-mono text-xs">
                  <span className="text-warning">{c.sha.slice(0, 7)}</span>
                  <span className="flex-1 truncate text-ink">{c.subject}</span>
                  <span className="text-ink-muted">{c.author}</span>
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <p className="py-3 text-sm text-ink-muted">
            This server is up to date with <code>origin/master</code>.
          </p>
        )}
      </SettingsGroup>

      {s.hasFailure && (
        <SettingsGroup legend="Last deploy" tone="danger">
          <div className="space-y-1 py-3">
            <DeployFailureNotice state={s} />
          </div>
        </SettingsGroup>
      )}
    </SettingsSection>
  )
}
