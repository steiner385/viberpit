/**
 * "Assistant gaps" — what users asked the AI assistant for that no tool could
 * do (its `report_gap` tool, aggregated server-side from `assistant.gap` audit
 * rows). Lives in the CI scope's welcome pane, the heir of the old Insights
 * page, and reuses the CI tables' `CheckTable` chassis.
 */
import { useAssistantGaps, type AssistantGap } from '@/api/assistant'
import { isValidTimestamp, relTime } from '@/lib/relativeTime'
import { Banner } from '@/ui/Banner'
import { Skeleton } from '@/ui/Skeleton'
import { CheckTable, type Column } from './CheckTable'

const DAYS = 30

const COLUMNS: Column<AssistantGap>[] = [
  { header: 'Capability', cell: (r) => r.capability },
  { header: 'Reports', cell: (r) => r.count },
  {
    header: 'Last seen',
    cell: (r) =>
      isValidTimestamp(r.lastSeen)
        ? <time dateTime={r.lastSeen} title={new Date(r.lastSeen).toLocaleString()}>{relTime(r.lastSeen)}</time>
        : '—',
  },
]

export function AssistantGapsTable() {
  const { data, isLoading, isError } = useAssistantGaps(DAYS)

  const placeholder = isError
    ? <Banner tone="danger" title="Could not load assistant gaps." />
    : isLoading && !data
      ? (
        <div role="status" aria-label="Loading assistant gaps">
          <Skeleton />
        </div>
      )
      : undefined

  return (
    <CheckTable
      title="Assistant gaps"
      description={`Requests the assistant could not act on, last ${DAYS} days`}
      columns={COLUMNS}
      rows={data?.gaps ?? []}
      empty={`No gaps reported in the last ${DAYS} days.`}
      placeholder={placeholder}
    />
  )
}
