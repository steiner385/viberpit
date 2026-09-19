/**
 * Delivery → Capacity, detail pane (design 2026-09-19 §7.1). Always what's
 * open for the `capacity` scope — there is no per-item route, so `DeliveryPage`
 * renders this whenever `scope === 'capacity'` and nothing else is open.
 * Self-contained: reads `GET /api/capacity` itself (owns the 30 s poll) rather
 * than taking the readout as a prop, the same shape `AutomationDetail` and
 * `CiRepoDetail` already use for their own data.
 */
import { useMemo, useState } from 'react'
import { ChevronDown, ChevronUp } from 'lucide-react'
import { Link } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { ApiRequestError } from '@/api/client'
import type { CapacityDefect, CapacityProduct, PushLevel } from '@/api/capacity'
import {
  QK, useAutomations, useCapacity, usePatchAutomation, useProject,
  useReorderProject, useUpdateProductPolicy,
} from '@/api/hooks'
import { updateProductPolicy, type Project } from '@/api/pm'
import { DELIVERY } from '@/features/products/vocab'
import { SaveBar } from '@/features/settings/chassis/SaveBar'
import { useSettingsForm } from '@/features/settings/useSettingsForm'
import { isValidTimestamp, relTime } from '@/lib/relativeTime'
import { useNow } from '@/hooks/useNow'
import { sessionPath } from '@/session/sessionNavState'
import { Badge } from '@/ui/Badge'
import { Banner } from '@/ui/Banner'
import { IconButton } from '@/ui/IconButton'
import { Input } from '@/ui/Input'
import { RadioGroup, type RadioOption } from '@/ui/RadioGroup'
import { Select } from '@/ui/Select'
import { Skeleton } from '@/ui/Skeleton'
import { Switch } from '@/ui/Switch'
import { buildCapacityStack } from './capacityStack'
import { RowStatus, type MutationLike, type RowStatusProps } from './RowStatus'
import { swapRanks } from './swapRanks'

const CAPACITY_GOVERNOR_ID = 'capacity-governor'

/** `{ status, error }` straight off a mutation object — every call site below
 *  is a plain TanStack Query mutation result, so this is never anything but
 *  a field pick. A local (non-exported) helper: `RowStatus.tsx` exports only
 *  the component and pure types, never a second value export, which is what
 *  keeps it out of Fast Refresh's "only export components" rule. */
function mutationStatus(mutation: MutationLike): RowStatusProps {
  return { status: mutation.status, error: mutation.error }
}

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
  product, index, total, defect, rankStatus, onMove, pinValue, onPinChange,
}: {
  product: CapacityProduct
  index: number
  total: number
  defect: CapacityDefect | null
  rankStatus: RowStatusProps
  onMove: (index: number, direction: 'up' | 'down') => void
  pinValue: string
  onPinChange: (value: string) => void
}) {
  const slug = product.slug
  const name = product.name ?? slug
  // Every visible row, not gated behind a "touched" flag any more (fix round
  // 1 / C2): the Switch's PATCH needs the concurrency stamp at the moment the
  // operator flips it, and a flag that only started the fetch on first
  // interaction meant that moment usually raced an empty cache. `poll: false`
  // keeps this from opening N 15s timers (one per row) over `QK.project` —
  // `staleTime` alone (5s) is enough for a value refetched on mount/focus.
  const { data: project } = useProject(slug, { poll: false })

  const patchPolicy = useUpdateProductPolicy(slug)
  const patchPolicyStatus = mutationStatus(patchPolicy)
  const pinned = product.capacityMode === 'pinned'

  return (
    <tr data-testid={`capacity-product-${slug}`}>
      <td className="whitespace-nowrap px-2 py-1.5">
        <div className="flex items-center gap-1">
          <IconButton
            aria-label={`Move ${name} up`}
            size="sm"
            disabled={index === 0}
            onClick={() => onMove(index, 'up')}
          >
            <ChevronUp size={14} aria-hidden="true" />
          </IconButton>
          <IconButton
            aria-label={`Move ${name} down`}
            size="sm"
            disabled={index === total - 1}
            onClick={() => onMove(index, 'down')}
          >
            <ChevronDown size={14} aria-hidden="true" />
          </IconButton>
          <span className="tabular-nums text-xs text-ink-muted">{product.rank ?? '—'}</span>
        </div>
        <RowStatus {...rankStatus} />
      </td>
      <th scope="row" className="px-2 py-1.5 text-left font-normal">
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
      </th>
      <td className="px-2 py-1.5 text-sm text-ink-muted">{product.dispatch}</td>
      <td className="px-2 py-1.5">
        <div className="flex items-center gap-2">
          <Switch
            aria-label={`${name} pinned`}
            checked={pinned}
            onChange={(next) => {
              const stamp = project?.automationPolicyUpdatedAt
              patchPolicy.mutate({
                policy: { capacity_mode: next ? 'pinned' : 'auto' },
                ...(stamp ? { expectedUpdatedAt: stamp } : {}),
              })
            }}
          />
          <Badge tone={pinned ? 'accent' : 'neutral'}>{pinned ? 'pinned' : 'auto'}</Badge>
          <RowStatus {...patchPolicyStatus} />
        </div>
      </td>
      <td className="px-2 py-1.5">
        <Input
          aria-label={`${name} pin`}
          type="number"
          size="sm"
          className="w-16"
          min={0}
          step={1}
          disabled={!pinned}
          value={pinValue}
          onChange={(e) => onPinChange(e.target.value)}
        />
      </td>
      <td className="px-2 py-1.5 text-right tabular-nums text-sm text-ink">{product.effectiveCap}</td>
      <td className="px-2 py-1.5 text-right tabular-nums text-sm text-ink">{product.inFlight}</td>
      <td className="px-2 py-1.5 text-right tabular-nums text-sm text-ink">{product.eligible}</td>
      <td className="px-2 py-1.5 text-xs text-ink-muted">{product.waitingOn ?? '—'}</td>
    </tr>
  )
}

export function CapacityDetail() {
  const qc = useQueryClient()
  const capacityQuery = useCapacity({ poll: true })
  const automations = useAutomations()
  // Two independent instances (not one shared `usePatchAutomation()`) so the
  // push-level radio and the governor-mode select each show THEIR OWN
  // Saving/Saved/Failed status rather than borrowing whichever one the
  // operator touched last.
  const pushLevelPatch = usePatchAutomation()
  const governorModePatch = usePatchAutomation()
  const pushLevelStatus = mutationStatus(pushLevelPatch)
  const governorModeStatus = mutationStatus(governorModePatch)
  const governor = automations.data?.find((a) => a.id === CAPACITY_GOVERNOR_ID) ?? null

  const reorder = useReorderProject()
  const [rankStatus, setRankStatus] = useState<Record<string, RowStatusProps>>({})
  const setSlugStatus = (slug: string, s: RowStatusProps) => {
    setRankStatus((prev) => ({ ...prev, [slug]: s }))
  }

  const readout = capacityQuery.data

  // Fix round 1 / I5: replaces the previous `moving` state + effect
  // workaround. `useReorderProject()` takes `slug` as a MUTATION VARIABLE
  // (not baked into the hook at creation time like `useUpdateProject(slug)`),
  // so one shared instance can `mutateAsync` for the mover and then the
  // neighbour, in order, entirely inside the click handler — no render round
  // trip needed to "rebind" a hook to a different slug. The second write
  // never fires if the first fails (Ruling 10).
  async function handleMove(index: number, direction: 'up' | 'down') {
    if (!readout) return
    const patches = swapRanks(readout.products, index, direction)
    if (!patches) return
    const [mover, neighbour] = patches
    setSlugStatus(mover.slug, { status: 'pending' })
    try {
      await reorder.mutateAsync({ slug: mover.slug, priorityRank: mover.priorityRank })
    } catch (err) {
      setSlugStatus(mover.slug, { status: 'error', error: err })
      return
    }
    setSlugStatus(mover.slug, { status: 'success' })
    setSlugStatus(neighbour.slug, { status: 'pending' })
    try {
      await reorder.mutateAsync({ slug: neighbour.slug, priorityRank: neighbour.priorityRank })
      setSlugStatus(neighbour.slug, { status: 'success' })
    } catch (err) {
      setSlugStatus(neighbour.slug, { status: 'error', error: err })
    }
  }

  // Ruling 12: the pin column is ONE form — a `Record<slug, string>` draft
  // seeded from the readout, saved as one sequential PATCH per dirty row
  // (stopping at the first failure), with the existing `SaveBar` doing the
  // dirty/saving/saved chrome instead of a Save button per row.
  const pinInitial = useMemo(
    () => (readout ? Object.fromEntries(readout.products.map((p) => [p.slug, String(p.pin)])) : undefined),
    [readout],
  )
  const pinForm = useSettingsForm<Record<string, string>>({
    initial: pinInitial,
    save: async (draft) => {
      if (!readout) return
      for (const product of readout.products) {
        const raw = draft[product.slug]
        if (raw === undefined) continue
        const name = product.name ?? product.slug
        // Fix round 2 / item 2: a cleared field is not "0" — `Number('')` is
        // 0, and silently saving that PAUSES the product (`max_in_flight: 0`
        // is a real, load-bearing value, not "no input yet"). Checked before
        // the unchanged-value skip below so a pin that happens to already be
        // 0 still can't be "saved" by clearing and re-submitting.
        if (raw.trim() === '') {
          throw new Error(`${name}: pin must be a whole number ≥ 0`)
        }
        const next = Number(raw)
        if (next === product.pin) continue
        if (!Number.isInteger(next) || next < 0) {
          throw new Error(`${name}: pin must be a whole number ≥ 0`)
        }
        // Fix round 1 / C2: never send `expectedUpdatedAt: null` — the stamp
        // is read straight off the query cache each row's own `useProject`
        // already populated (a plain cache read here, not a second hook call
        // in a loop, which the rules of hooks forbid).
        const stamp = qc.getQueryData<Project>(QK.project(product.slug))?.automationPolicyUpdatedAt
        let project: Project
        try {
          project = await updateProductPolicy(product.slug, {
            policy: { max_in_flight: next },
            ...(stamp ? { expectedUpdatedAt: stamp } : {}),
          })
        } catch (err) {
          // Fix round 2 / item 1: mirrors `useUpdateProductPolicy`'s onError
          // — a 409 means the cached stamp is stale, so refresh it before
          // the caller (`useSettingsForm`) surfaces the failure, rather than
          // leaving the next attempt to fail the same way again.
          if (err instanceof ApiRequestError && err.status === 409) {
            await qc.invalidateQueries({ queryKey: QK.project(product.slug) })
          }
          throw err
        }
        // Fix round 2 / item 1: keep the response's fresh
        // `automationPolicyUpdatedAt` so the NEXT toggle or save on this row
        // (cap-mode switch or another pin edit) sends the current stamp
        // instead of the one that was already just spent.
        qc.setQueryData(QK.project(product.slug), project)
      }
      qc.invalidateQueries({ queryKey: QK.capacity })
      qc.invalidateQueries({ queryKey: QK.roadmapMap })
    },
  })

  const now = useNow(60_000, isValidTimestamp(readout?.plan?.computedAt))

  if (capacityQuery.isError) {
    return <Banner tone="danger" title={DELIVERY.capacityError} />
  }
  if (!readout) {
    return <Skeleton lines={6} />
  }

  const stack = buildCapacityStack(readout)
  const plan = readout.plan
  const governorDescription = governor?.mode ? governor.oversight?.modeDescriptions[governor.mode] : undefined

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-start gap-8">
        <div className="flex flex-col gap-1.5">
          <RadioGroup
            name="push-level"
            legend="Push level"
            value={readout.pushLevel}
            onChange={(next) => pushLevelPatch.mutate({
              id: CAPACITY_GOVERNOR_ID, body: { config: { push_level: next } },
            })}
            options={PUSH_LEVEL_OPTIONS}
          />
          <RowStatus {...pushLevelStatus} />
        </div>
        <div className="flex flex-col gap-1.5">
          <label htmlFor="governor-mode" className="text-sm font-medium text-ink">Governor mode</label>
          <Select
            id="governor-mode"
            size="sm"
            value={governor?.mode ?? ''}
            disabled={!governor}
            onChange={(e) => governor && governorModePatch.mutate({
              id: governor.id, body: { mode: e.target.value },
            })}
          >
            {(governor?.modes ?? []).map((m) => <option key={m} value={m}>{m}</option>)}
          </Select>
          {governor
            ? governorDescription && <p className="text-xs text-ink-muted">{governorDescription}</p>
            : <p className="text-xs text-ink-muted">Capacity Governor is not registered</p>}
          <RowStatus {...governorModeStatus} />
        </div>
      </div>

      <ul className="flex flex-col gap-1" aria-label="Capacity stack">
        {stack.map((row) => {
          const content = (
            <div>
              <div className="flex items-center justify-between gap-2 text-sm">
                {row.href
                  ? <Link to={row.href} className="text-accent hover:underline">{row.label}</Link>
                  : <span className="text-ink">{row.label}</span>}
                <span className="tabular-nums text-ink-muted">{row.used} / {row.cap ?? '∞'} · {row.free ?? '∞'} free</span>
              </div>
              {row.secondary && <p className="text-xs text-ink-muted">{row.secondary}</p>}
            </div>
          )
          return (
            <li key={row.key} data-testid={`capacity-stack-${row.key}`} className="px-1 py-1">
              {row.binding && readout.binding
                // `role="none"`: this content is a live-refreshing figure (the
                // stack polls every 30s), not a one-shot announcement — see
                // Banner's own docstring on why a changing region must opt out
                // of being a live region rather than spam a screen reader.
                ? <Banner tone="warning" role="none" title={readout.binding.text}>{content}</Banner>
                : content}
            </li>
          )
        })}
      </ul>

      <div className="overflow-x-auto">
        <table className="w-full text-left" aria-label="Products">
          <thead>
            <tr className="text-xs font-medium text-ink-muted">
              <th scope="col" className="px-2 py-1.5">Rank</th>
              <th scope="col" className="px-2 py-1.5">Product</th>
              <th scope="col" className="px-2 py-1.5">Dispatch</th>
              <th scope="col" className="px-2 py-1.5">Cap mode</th>
              <th scope="col" className="px-2 py-1.5">Pin</th>
              <th scope="col" className="px-2 py-1.5 text-right">Effective cap</th>
              <th scope="col" className="px-2 py-1.5 text-right">In flight</th>
              <th scope="col" className="px-2 py-1.5 text-right">Eligible</th>
              <th scope="col" className="px-2 py-1.5">Waiting on</th>
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
                rankStatus={rankStatus[product.slug] ?? { status: 'idle' }}
                onMove={handleMove}
                pinValue={pinForm.draft?.[product.slug] ?? String(product.pin)}
                onPinChange={(value) => pinForm.set(product.slug, value)}
              />
            ))}
          </tbody>
        </table>
        <SaveBar form={pinForm} />
        {pinForm.error && <Banner tone="danger" title={pinForm.error} />}
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
