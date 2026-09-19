/**
 * The five ceiling-stack rows (design 2026-09-19 §7.1: Global cap → Hosts →
 * Accounts → Runner ceiling → Products), built once here so the list column's
 * `CapacityScopeBody` and the detail pane's `CapacityDetail` render the same
 * numbers from the same fields rather than two hand-rolled reductions that
 * could drift.
 */
import type { CapacityBindingLayer, CapacityReadout } from '@/api/capacity'
import { automationPath } from '../deliveryNavState'

export interface CapacityStackRow {
  key: 'global' | 'hosts' | 'accounts' | 'runner' | 'products'
  label: string
  used: number
  /** null only for the Hosts row, when at least one enabled host has no max. */
  cap: number | null
  free: number | null
  /** The settings/automation page that owns this row's dial, or null when
   *  there is none (Products — its own dial is the table on this same page). */
  href: string | null
  binding: boolean
}

function isBindingLayer(binding: CapacityReadout['binding'], ...layers: CapacityBindingLayer[]): boolean {
  return binding !== null && layers.includes(binding.layer)
}

export function buildCapacityStack(r: CapacityReadout): CapacityStackRow[] {
  const hostsUnbounded = r.hosts.some((h) => h.enabled && h.max === null)
  const hostsCap = hostsUnbounded ? null : r.hosts.reduce((s, h) => s + (h.max ?? 0), 0)
  const hostsFree = hostsUnbounded ? null : r.hosts.reduce((s, h) => s + (h.free ?? 0), 0)
  const hostsActive = r.hosts.reduce((s, h) => s + h.active, 0)
  const accountsUsed = Math.max(0, r.accounts.total - r.accounts.available)
  const runnerFree = Math.max(0, r.runner.ceiling - r.runner.inFlight)
  const productsCap = r.products.reduce((s, p) => s + p.effectiveCap, 0)
  const productsUsed = r.products.reduce((s, p) => s + p.inFlight, 0)

  return [
    {
      key: 'global', label: 'Global cap', used: r.global.active, cap: r.global.cap, free: r.global.free,
      href: '/settings/general', binding: isBindingLayer(r.binding, 'global'),
    },
    {
      key: 'hosts', label: 'Hosts', used: hostsActive, cap: hostsCap, free: hostsFree,
      href: '/settings/fleet', binding: isBindingLayer(r.binding, 'host'),
    },
    {
      key: 'accounts', label: 'Accounts', used: accountsUsed, cap: r.accounts.total, free: r.accounts.available,
      href: '/settings/accounts', binding: isBindingLayer(r.binding, 'account'),
    },
    {
      key: 'runner', label: 'Runner ceiling', used: r.runner.inFlight, cap: r.runner.ceiling, free: runnerFree,
      href: `${automationPath('backlog-runner')}?tab=config`, binding: isBindingLayer(r.binding, 'runner'),
    },
    {
      key: 'products', label: 'Products', used: productsUsed, cap: productsCap,
      free: Math.max(0, productsCap - productsUsed),
      href: null, binding: isBindingLayer(r.binding, 'product', 'defect'),
    },
  ]
}
