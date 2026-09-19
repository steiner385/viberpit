/**
 * Delivery → Capacity, detail pane (design 2026-09-19 §7.1). Always what's
 * open for the `capacity` scope — there is no per-item route, so `DeliveryPage`
 * renders this whenever `scope === 'capacity'` and nothing else is open.
 * Self-contained: reads `GET /api/capacity` itself (owns the 30 s poll) rather
 * than taking the readout as a prop, the same shape `AutomationDetail` and
 * `CiRepoDetail` already use for their own data.
 */
import { useEffect, useState } from 'react'
import { ChevronDown, ChevronUp } from 'lucide-react'
import { Link } from 'react-router-dom'
import type { CapacityDefect, CapacityProduct, PushLevel } from '@/api/capacity'
import { useAutomations, useCapacity, usePatchAutomation, useProject, useUpdateProductPolicy, useUpdateProject } from '@/api/hooks'
import { DELIVERY } from '@/features/products/vocab'
import { isValidTimestamp, relTime } from '@/lib/relativeTime'
import { useNow } from '@/hooks/useNow'
import { sessionPath } from '@/session/sessionNavState'
import { Badge } from '@/ui/Badge'
import { Banner } from '@/ui/Banner'
import { Button } from '@/ui/Button'
import { IconButton } from '@/ui/IconButton'
import { Input } from '@/ui/Input'
import { RadioGroup, type RadioOption } from '@/ui/RadioGroup'
import { Select } from '@/ui/Select'
import { Skeleton } from '@/ui/Skeleton'
import { Switch } from '@/ui/Switch'
import { buildCapacityStack } from './capacityStack'
import { swapRanks, type RankPatch } from './swapRanks'

const CAPACITY_GOVERNOR_ID = 'capacity-governor'

// Mirrors RESERVE_PCT in server/automations/capacity_plan.py (design §4) —
// display only, never read back from here.
const PUSH_LEVEL_RESERVE_PCT: Record<PushLevel, number> = { conserve: 40, steady: 20, max: 5 }
const PUSH_LEVEL_LABEL: Record<PushLevel, string> = { conserve: 'Conserve', steady: 'Steady', max: 'Max' }
const PUSH_LEVEL_OPTIONS: RadioOption<PushLevel>[] = (['conserve', 'steady', 'max'] as const).map((v) => ({
  value: v,
  label: PUSH_LEVEL_LABEL[v],
  description: `${PUSH_LEVEL_RESERVE_PCT[v]}% weekly reserve`,
}))

function CapacityProductRow({
  product, index, total, defect, onMove,
}: {
  product: CapacityProduct
  index: number
  total: number
  defect: CapacityDefect | null
  onMove: (index: number, direction: 'up' | 'down') => void
}) {
  const slug = product.slug
  const name = product.name ?? slug
  // Lazy: the readout carries no concurrency stamp for the policy PATCH, so
  // the row only reads the project (and its `automationPolicyUpdatedAt`) once
  // the operator actually starts editing this row, not for every row in the
  // table on every open of the page.
  const [touched, setTouched] = useState(false)
  const { data: project } = useProject(touched ? slug : null)
  const expectedUpdatedAt = project?.automationPolicyUpdatedAt ?? null

  const patchPolicy = useUpdateProductPolicy(slug)
  const pinned = product.capacityMode === 'pinned'
  const [pinDraft, setPinDraft] = useState(String(product.pin))

  return (
    <tr data-testid={`capacity-product-${slug}`}>
      <td className="whitespace-nowrap px-2 py-1.5">
        <div className="flex items-center gap-1">
          <IconButton
            aria-label="Move up"
            size="sm"
            disabled={index === 0}
            onClick={() => onMove(index, 'up')}
          >
            <ChevronUp size={14} aria-hidden="true" />
          </IconButton>
          <IconButton
            aria-label="Move down"
            size="sm"
            disabled={index === total - 1}
            onClick={() => onMove(index, 'down')}
          >
            <ChevronDown size={14} aria-hidden="true" />
          </IconButton>
          <span className="tabular-nums text-xs text-ink-muted">{product.rank ?? '—'}</span>
        </div>
      </td>
      <td className="px-2 py-1.5">
        <div className="flex items-center gap-2">
          <span className="truncate text-sm text-ink">{name}</span>
          {product.defect && (
            <Badge tone="danger" title={defect?.text}>
              {defect
                ? <Link to={sessionPath(defect.sessionId)} className="hover:underline">defect</Link>
                : 'defect'}
            </Badge>
          )}
        </div>
      </td>
      <td className="px-2 py-1.5 text-sm text-ink-muted">{product.dispatch}</td>
      <td className="px-2 py-1.5">
        <div className="flex items-center gap-2">
          <Switch
            aria-label={`${name} pinned`}
            checked={pinned}
            onChange={(next) => {
              setTouched(true)
              patchPolicy.mutate({
                policy: { capacity_mode: next ? 'pinned' : 'auto' },
                expectedUpdatedAt,
              })
            }}
          />
          <Badge tone={pinned ? 'accent' : 'neutral'}>{pinned ? 'pinned' : 'auto'}</Badge>
        </div>
      </td>
      <td className="px-2 py-1.5">
        <div className="flex items-center gap-1.5">
          <Input
            aria-label={`${name} pin`}
            type="number"
            size="sm"
            className="w-16"
            disabled={!pinned}
            value={pinDraft}
            onFocus={() => setTouched(true)}
            onChange={(e) => setPinDraft(e.target.value)}
          />
          <Button
            type="button"
            variant="secondary"
            size="sm"
            disabled={!pinned || Number(pinDraft) === product.pin || pinDraft === ''}
            onClick={() => patchPolicy.mutate({
              policy: { max_in_flight: Number(pinDraft) },
              expectedUpdatedAt,
            })}
          >
            Save
          </Button>
        </div>
      </td>
      <td className="px-2 py-1.5 text-right tabular-nums text-sm text-ink">{product.effectiveCap}</td>
      <td className="px-2 py-1.5 text-right tabular-nums text-sm text-ink">{product.inFlight}</td>
      <td className="px-2 py-1.5 text-right tabular-nums text-sm text-ink">{product.eligible}</td>
      <td className="px-2 py-1.5 text-xs text-ink-muted">{product.waitingOn ?? '—'}</td>
    </tr>
  )
}

export function CapacityDetail() {
  const capacityQuery = useCapacity({ poll: true })
  const automations = useAutomations()
  const patch = usePatchAutomation()
  const governor = automations.data?.find((a) => a.id === CAPACITY_GOVERNOR_ID) ?? null

  const [moving, setMoving] = useState<{ mover: RankPatch; neighbour: RankPatch } | null>(null)
  const moverMutation = useUpdateProject(moving?.mover.slug ?? '')
  const neighbourMutation = useUpdateProject(moving?.neighbour.slug ?? '')

  // Fires the two rank patches one render after `handleMove` sets `moving` —
  // only then are `moverMutation`/`neighbourMutation` bound to the right
  // slugs (see `useUpdateProject`'s per-slug closure). Never reset to null:
  // nothing renders from `moving` itself, and each click produces a brand new
  // object (`swapRanks` never returns a cached one), so the next move's own
  // identity change is what re-fires this effect — clearing it would only add
  // a second setState with nothing observing it.
  useEffect(() => {
    if (!moving) return
    moverMutation.mutate({ priorityRank: moving.mover.priorityRank })
    neighbourMutation.mutate({ priorityRank: moving.neighbour.priorityRank })
    // eslint-disable-next-line react-hooks/exhaustive-deps -- fires once per `moving` identity, not per mutation-object identity
  }, [moving])

  const now = useNow(60_000, isValidTimestamp(capacityQuery.data?.plan?.computedAt))

  if (capacityQuery.isError) {
    return <Banner tone="danger" title={DELIVERY.capacityError} />
  }
  const readout = capacityQuery.data
  if (!readout) {
    return <Skeleton lines={6} />
  }

  const stack = buildCapacityStack(readout)
  const plan = readout.plan

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-start gap-8">
        <RadioGroup
          name="push-level"
          legend="Push level"
          value={readout.pushLevel}
          onChange={(next) => patch.mutate({ id: CAPACITY_GOVERNOR_ID, body: { config: { push_level: next } } })}
          options={PUSH_LEVEL_OPTIONS}
        />
        <div className="flex flex-col gap-1.5">
          <label htmlFor="governor-mode" className="text-sm font-medium text-ink">Governor mode</label>
          <Select
            id="governor-mode"
            aria-label="Governor mode"
            size="sm"
            value={governor?.mode ?? ''}
            disabled={!governor}
            title={governor ? governor.oversight?.modeDescriptions[governor.mode ?? ''] : 'Capacity Governor is not registered'}
            onChange={(e) => governor && patch.mutate({ id: governor.id, body: { mode: e.target.value } })}
          >
            {(governor?.modes ?? []).map((m) => <option key={m} value={m}>{m}</option>)}
          </Select>
          {!governor && <p className="text-xs text-ink-muted">Capacity Governor is not registered</p>}
        </div>
      </div>

      <ul className="flex flex-col gap-1" aria-label="Capacity stack">
        {stack.map((row) => {
          const content = (
            <div className="flex items-center justify-between gap-2 text-sm">
              {row.href
                ? <Link to={row.href} className="text-accent hover:underline">{row.label}</Link>
                : <span className="text-ink">{row.label}</span>}
              <span className="tabular-nums text-ink-muted">{row.used} / {row.cap ?? '∞'} · {row.free ?? '∞'} free</span>
            </div>
          )
          return (
            <li key={row.key} data-testid={`capacity-stack-${row.key}`} className="px-1 py-1">
              {row.binding && readout.binding
                ? <Banner tone="warning" title={readout.binding.text}>{content}</Banner>
                : content}
            </li>
          )
        })}
      </ul>

      <div className="overflow-x-auto">
        <table className="w-full text-left">
          <thead>
            <tr className="text-xs font-medium text-ink-muted">
              <th className="px-2 py-1.5">Rank</th>
              <th className="px-2 py-1.5">Product</th>
              <th className="px-2 py-1.5">Dispatch</th>
              <th className="px-2 py-1.5">Cap mode</th>
              <th className="px-2 py-1.5">Pin</th>
              <th className="px-2 py-1.5 text-right">Effective cap</th>
              <th className="px-2 py-1.5 text-right">In flight</th>
              <th className="px-2 py-1.5 text-right">Eligible</th>
              <th className="px-2 py-1.5">Waiting on</th>
            </tr>
          </thead>
          <tbody>
            {readout.products.map((product, index) => (
              <CapacityProductRow
                key={product.slug}
                product={product}
                index={index}
                total={readout.products.length}
                defect={readout.defects.find((d) => d.product === product.slug) ?? null}
                onMove={(i, direction) => {
                  const patches = swapRanks(readout.products, i, direction)
                  if (patches) setMoving({ mover: patches[0], neighbour: patches[1] })
                }}
              />
            ))}
          </tbody>
        </table>
      </div>

      {plan && (
        <div className="rounded-lg border border-edge p-4">
          <p className="text-sm font-medium text-ink">
            {plan.applied ? 'Governor set…' : 'Governor would set…'}
          </p>
          {plan.reasons.length > 0 && (
            <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-ink-muted">
              {plan.reasons.map((reason, i) => <li key={i}>{reason}</li>)}
            </ul>
          )}
          {isValidTimestamp(plan.computedAt) && (
            <p className="mt-2 text-xs text-ink-faint" title={plan.computedAt}>
              {relTime(plan.computedAt, now || undefined)}
            </p>
          )}
        </div>
      )}
    </div>
  )
}
