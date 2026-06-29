// web/src/components/CheckDrillDown.tsx
import { CheckRowList } from '@/components/CheckRowList'
import { formatEta } from '@/lib/formatEta'
import type { CheckGroup } from '@/api/types'

const GROUP_LABEL: Record<CheckGroup['kind'], string> = {
  merge: 'MERGE GROUP BUILD',
  head: 'PR CHECKS (HEAD COMMIT)',
}
const GROUP_ORDER: CheckGroup['kind'][] = ['merge', 'head']

export function CheckDrillDown({ groups }: { groups: CheckGroup[] }) {
  const ordered = [...groups].sort(
    (a, b) => GROUP_ORDER.indexOf(a.kind) - GROUP_ORDER.indexOf(b.kind),
  )
  return (
    <div className="space-y-3 px-2 py-2">
      {ordered.map((grp) => (
        <section key={grp.kind}>
          <h4 className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
            {GROUP_LABEL[grp.kind]}
          </h4>
          {grp.cost_usd != null && (
            <p data-testid={`ci-cost-${grp.kind}`} className="mb-1 text-[11px] text-muted-foreground">
              CI cost this run: {formatEta(grp.total_seconds)} (~${grp.cost_usd})
            </p>
          )}
          <CheckRowList checks={grp.checks} />
        </section>
      ))}
    </div>
  )
}
