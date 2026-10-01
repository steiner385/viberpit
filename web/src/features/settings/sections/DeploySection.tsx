import { Button } from '@/ui/Button'
import { useDeployStatus, usePendingDeploy } from '@/api/hooks'
import { useDeployState } from '@/lib/deployState'
import { useDeployController } from '@/lib/deployController'
import { deployOutcome } from '@/lib/deployPhases'
import { DeployFailureNotice } from '@/shell/DeployFailureNotice'
import { SETTINGS_BY_ID } from '../settingsDefs'
import { SettingsSection } from '../chassis/SettingsSection'
import { SettingsGroup } from '../chassis/SettingsGroup'

/**
 * Deploy, at full size — and on a phone, the ONLY size.
 *
 * The rail-foot `DeployChip` points here while commits are pending (it took
 * over from the top-of-page banner, 2026-10-01 shell critique, P2); the
 * app-level `DeployPanel` banner carries only a FAILURE, and not on phone
 * (`hideOn: ['phone']` in `shell/useAppBanners`). All three read
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
  const { data: deploy } = useDeployStatus()
  const { data: pending } = usePendingDeploy()
  const outcome = deployOutcome(deploy)
  const restored = deploy?.restore?.restored?.length ?? null
  const liveSha = pending?.liveSha ? pending.liveSha.slice(0, 7) : null

  return (
    <SettingsSection id="deploy" title="Deploy" scope="server"
      description={SETTINGS_BY_ID.deploy.description}>
      <SettingsGroup legend="Pending changes">
        {s.behind ? (
          <div className="flex min-w-0 flex-col gap-3 py-3">
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
                <li key={c.sha} className="flex min-w-0 gap-2 font-mono text-xs">
                  <span className="text-warning">{c.sha.slice(0, 7)}</span>
                  <span className="min-w-0 flex-1 truncate text-ink">{c.subject}</span>
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

      {/* The description promises "how the last deploy went"; until now only a
          failure was ever rendered, so a clean deploy left the promise empty
          (2026-09-30 Settings critique). The controller reports no timestamp,
          so this says what it knows: it completed, and how many sessions came
          back. It yields to the failure group below whenever anything is wrong. */}
      {!s.running && !s.hasFailure && outcome === 'success' && (
        <SettingsGroup legend="Last deploy">
          <p className="py-3 text-sm text-ink-muted" data-testid="deploy-last-success">
            Completed{restored != null ? ` — ${restored} session${restored === 1 ? '' : 's'} restored` : ''}.
            {' '}Running <code>{liveSha}</code>.
          </p>
        </SettingsGroup>
      )}

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
