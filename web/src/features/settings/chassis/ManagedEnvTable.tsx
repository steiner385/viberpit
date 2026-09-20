import type { ManagedFlag } from '@/api/hooks'

// eslint-disable-next-line react-refresh/only-export-components
export function showManagedValue(v: ManagedFlag['value']): string {
  if (v === null) return 'unset'
  if (typeof v === 'boolean') return v ? 'on' : 'off'
  return String(v)
}

/**
 * Read-only env flags (spec 2026-09-04 S30). About shows the whole allowlist;
 * Limits shows the capacity subset (spec 2026-09-20 D9) — one table, one
 * rendering, so a flag never looks different on the two pages.
 */
export function ManagedEnvTable({ flags, keys }: { flags: ManagedFlag[]; keys?: readonly string[] }) {
  const rows = keys
    ? keys.map((k) => flags.find((f) => f.key === k)).filter((f): f is ManagedFlag => f !== undefined)
    : flags
  return (
    <div className="overflow-x-auto py-2">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs uppercase tracking-wide text-ink-muted">
            <th scope="col" className="py-1 pr-4 font-medium">Variable</th>
            <th scope="col" className="py-1 pr-4 font-medium">Value</th>
            <th scope="col" className="py-1 font-medium">What it does</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((m) => (
            <tr key={m.key} className="border-t border-edge">
              <th scope="row" className="py-1.5 pr-4 font-mono text-xs font-normal text-ink">{m.key}</th>
              <td className="py-1.5 pr-4 font-mono text-xs">{showManagedValue(m.value)}</td>
              <td className="py-1.5 text-ink-muted">{m.description}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
