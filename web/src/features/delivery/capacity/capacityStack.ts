/**
 * The five ceiling-stack rows (design 2026-09-19 §7.1: Global cap, Hosts →
 * Accounts → Runner ceiling → Products), built once here so the list column's
 * `CapacityScopeBody` and the detail pane's `CapacityDetail` render the same
 * numbers from the same fields rather than two hand-rolled reductions that
 * could drift.
 */
import type { CapacityAccountRow, CapacityBindingLayer, CapacityReadout } from '@/api/capacity'
import { formatResetTime } from '@/lib/formatReset'
import { automationPath } from '../deliveryNavState'
import { settingsSectionPath } from '@/shell/navDefs'

export interface CapacityStackRow {
  key: 'global' | 'hosts' | 'accounts' | 'runner' | 'products'
  label: string
  used: number
  /** null only for the Hosts row, when at least one enabled host has no max. */
  cap: number | null
  free: number | null
  /** The page that owns this row's dial; Products uses an in-page anchor to its own table. */
  href: string | null
  binding: boolean
  /** Secondary line under the row — today only the Hosts row's excluded-host
   *  note, but generic so a future row can use it without a parallel field. */
  secondary?: string
}

function isBindingLayer(binding: CapacityReadout['binding'], ...layers: CapacityBindingLayer[]): boolean {
  return binding !== null && layers.includes(binding.layer)
}

const GLOBAL_BINDING_TEXT = { physical: 'physical binds', attention: 'attention binds', default: 'default floor' } as const

/** The Global row's second line (spec 2026-09-20 §5.2): both ceilings and which binds. */
function globalSecondary(g: CapacityReadout['global']): string | undefined {
  if (g.binding === null) return undefined
  return `physical ${g.physical ?? '∞'} · attention ${g.attention ?? 'none'} · ${GLOBAL_BINDING_TEXT[g.binding]}`
}

/** One per-account line for the Accounts row's expansion (C21). Reasons are the
 *  server's snake_case codes from `account_headroom`; spelled out here once. */
export function accountRowLine(row: CapacityAccountRow): string {
  if (row.available) return `${row.name} — available`
  const reason = (row.reason ?? 'unavailable').replace(/_/g, ' ')
  const reset = formatResetTime(row.nextResetAt)
  return reset ? `${row.name} — ${reason} · resets ${reset}` : `${row.name} — ${reason}`
}

export function buildCapacityStack(r: CapacityReadout): CapacityStackRow[] {
  // Mirrors `_fleet()` in server/automations/capacity_plan.py: a disabled or
  // not-confirmed-healthy host (`healthy` false or still `null`/unknown)
  // contributes nothing to the fleet's real capacity, so it must not inflate
  // this row's used/cap/free either — a host flapping unhealthy used to make
  // the Hosts row claim more capacity than the governor would actually use.
  const countedHosts = r.hosts.filter((h) => h.enabled && h.healthy === true)
  const excludedHosts = r.hosts.length - countedHosts.length
  const hostsUnbounded = countedHosts.some((h) => h.max === null)
  const hostsCap = hostsUnbounded ? null : countedHosts.reduce((s, h) => s + (h.max ?? 0), 0)
  const hostsFree = hostsUnbounded ? null : countedHosts.reduce((s, h) => s + (h.free ?? 0), 0)
  const hostsActive = countedHosts.reduce((s, h) => s + h.active, 0)
  const accountsUsed = Math.max(0, r.accounts.total - r.accounts.available)
  // `effective`, not `ceiling`: what the runner is actually enforcing right
  // now (the live plan's overlay), not the operator's stored value alone —
  // fix round 2, N1.
  const runnerFree = Math.max(0, r.runner.effective - r.runner.inFlight)
  const productsCap = r.products.reduce((s, p) => s + p.effectiveCap, 0)
  const productsUsed = r.products.reduce((s, p) => s + p.inFlight, 0)

  return [
    {
      key: 'global', label: 'Global cap', used: r.global.active, cap: r.global.cap, free: r.global.free,
      href: settingsSectionPath('capacity'), binding: isBindingLayer(r.binding, 'global'),
      secondary: globalSecondary(r.global),
    },
    {
      key: 'hosts', label: 'Hosts', used: hostsActive, cap: hostsCap, free: hostsFree,
      href: settingsSectionPath('fleet'), binding: isBindingLayer(r.binding, 'host'),
      secondary: excludedHosts > 0
        ? `${excludedHosts} host${excludedHosts === 1 ? '' : 's'} excluded: disabled or unhealthy`
        : undefined,
    },
    {
      key: 'accounts', label: 'Accounts', used: accountsUsed, cap: r.accounts.total, free: r.accounts.available,
      href: settingsSectionPath('accounts'), binding: isBindingLayer(r.binding, 'account'),
    },
    {
      key: 'runner', label: 'Runner ceiling', used: r.runner.inFlight, cap: r.runner.effective, free: runnerFree,
      href: `${automationPath('backlog-runner')}?tab=config`, binding: isBindingLayer(r.binding, 'runner'),
      secondary: r.runner.effective !== r.runner.ceiling
        ? `stored ceiling ${r.runner.ceiling}` : undefined,
    },
    {
      key: 'products', label: 'Products', used: productsUsed, cap: productsCap,
      free: Math.max(0, productsCap - productsUsed),
      href: '#products', binding: isBindingLayer(r.binding, 'product', 'defect'),
    },
  ]
}
