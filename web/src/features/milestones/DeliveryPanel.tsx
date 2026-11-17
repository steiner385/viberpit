// The side panel's Prod delivery (#3404): what production runs, how far behind the
// default branch it is, whether a gate workflow verified it, the Nightly streak, and
// the ci:regressed issues blocking promotion with who is on each. The page sets it
// right under Needs you; the ask it raises is listed there.
import type { MilestoneDelivery } from '@/api/milestones'
import { Badge, type Tone } from '@/ui/Badge'
import { SectionHeading } from '@/ui/SectionHeading'
import { isValidTimestamp, relTime } from '@/lib/relativeTime'

const STATE: Record<MilestoneDelivery['state'], { label: string; tone: Tone }> = {
  healthy: { label: 'Current', tone: 'success' },
  behind: { label: 'Behind', tone: 'info' },
  blocked: { label: 'Blocked', tone: 'danger' },
  unverified: { label: 'Unverified', tone: 'danger' },
  unknown: { label: 'Unknown', tone: 'neutral' },
}

function hours(h: number | null): string {
  if (h === null) return '?'
  return h >= 72 ? `${(h / 24).toFixed(1)} days` : `${Math.round(h)} h`
}

function behindLine(d: MilestoneDelivery): string {
  if (d.behindCommits === null) return `Distance from ${d.branch} not read`
  if (d.behindCommits === 0) return `Level with ${d.branch}`
  return `${d.behindCommits} commit${d.behindCommits === 1 ? '' : 's'} · ${hours(d.hoursBehind)} behind ${d.branch}`
}

function verifiedLine(d: MilestoneDelivery): string | null {
  if (d.verified === null) return d.gateWorkflows.length === 0 ? 'No gate workflows set' : null
  return d.verified ? 'Verified by a green gate run' : `Not verified by ${d.gateWorkflows.join(' or ')}`
}

function owner(r: { session: boolean; prs: string[] }): string {
  if (r.session) return 'session on it'
  if (r.prs.length > 0) return r.prs.join(', ')
  return 'nobody on it'
}

export function DeliveryPanel({ delivery }: { delivery: MilestoneDelivery | null | undefined }) {
  if (!delivery) return null
  const state = STATE[delivery.state] ?? STATE.unknown
  const sha = delivery.prodSha?.slice(0, 7)
  const verified = verifiedLine(delivery)
  return (
    <section aria-labelledby="milestones-delivery" className="flex flex-col gap-2" data-testid="delivery-panel">
      <SectionHeading id="milestones-delivery">Prod delivery</SectionHeading>
      <div className="flex flex-col gap-1.5 rounded-md border border-edge bg-surface p-3 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={state.tone}>{state.label}</Badge>
          {sha && (
            delivery.compareUrl
              ? <a href={delivery.compareUrl} target="_blank" rel="noreferrer" className="font-mono text-accent hover:underline">{sha}</a>
              : <span className="font-mono">{sha}</span>
          )}
        </div>
        {delivery.state === 'unknown' ? (
          <p className="text-ink-muted">The prod health endpoint did not answer.</p>
        ) : (
          <>
            <p className="text-ink-muted">{behindLine(delivery)}</p>
            {verified && (
              delivery.verified && delivery.verifiedBy
                ? <a href={delivery.verifiedBy} target="_blank" rel="noreferrer" className="text-accent hover:underline">{verified}</a>
                : <p className={delivery.verified === false ? 'text-danger' : 'text-ink-muted'}>{verified}</p>
            )}
          </>
        )}
        {delivery.streak !== null && (
          <p className="text-ink-muted">
            {delivery.gateWorkflows[0]} streak:{' '}
            {delivery.streakUrl
              ? <a href={delivery.streakUrl} target="_blank" rel="noreferrer" className="text-accent hover:underline">{delivery.streak} green</a>
              : `${delivery.streak} green`}
          </p>
        )}
        {delivery.regressed && delivery.regressed.length > 0 && (
          <ul aria-label="Regressed issues" className="flex flex-col gap-0.5 text-xs">
            {delivery.regressed.map((r) => (
              <li key={r.ref}>
                <a href={r.url} target="_blank" rel="noreferrer" className="text-accent hover:underline">{r.ref}</a>
                <span className={r.owned ? 'text-ink-muted' : 'text-danger'}> · {owner(r)}</span>
              </li>
            ))}
          </ul>
        )}
        <p className="text-xs text-ink-faint">
          {isValidTimestamp(delivery.measuredAt) ? relTime(delivery.measuredAt) : 'Never measured'}
          {delivery.error ? ` · ${delivery.error}` : ''}
        </p>
      </div>
    </section>
  )
}
