import { useBacklogGroups } from '@/api/hooks'
import type { BacklogGroup } from '@/api/automations'

function issueUrl(ref: string): string {
  const [repo, num] = ref.split('#')
  return `https://github.com/${repo}/issues/${num}`
}

function GroupRow({ group }: { group: BacklogGroup }) {
  return (
    <li className="rounded border border-border p-2">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
        {group.repo && (
          <span className="text-xs text-muted-foreground shrink-0">{group.repo}</span>
        )}
        {group.refs.map((ref) => (
          <a
            key={ref}
            href={issueUrl(ref)}
            target="_blank"
            rel="noreferrer"
            title={group.titles[ref]}
            className="hover:underline"
          >
            #{ref.split('#')[1]}
            {group.titles[ref] ? ` ${group.titles[ref]}` : ''}
          </a>
        ))}
        {group.consumedAt && (
          <span className="ml-auto rounded bg-blue-100 dark:bg-blue-900/40 px-1 text-xs text-blue-700 dark:text-blue-300 shrink-0">
            picked up
          </span>
        )}
      </div>
      {group.rationale && (
        <p className="mt-0.5 text-xs text-muted-foreground leading-snug">{group.rationale}</p>
      )}
    </li>
  )
}

/** Read-only view of the grouping optimizer's current plan: the PR-sized issue
 * groups the Backlog Runner will pull next, in priority order, with the
 * optimizer's rationale for each bundle. */
export function BacklogGroupsPanel({ enabled }: { enabled: boolean }) {
  const { data } = useBacklogGroups(enabled)
  const groups = data?.groups ?? []
  if (!groups.length) return null

  return (
    <section className="mt-3 border-t border-border pt-3">
      <h4 className="text-xs font-semibold uppercase text-muted-foreground mb-2">
        Planned groups
        {data?.createdAt && (
          <span className="ml-2 font-normal normal-case">
            planned {data.createdAt.slice(0, 16).replace('T', ' ')}
          </span>
        )}
      </h4>
      <ul className="space-y-2">
        {groups.map((g) => <GroupRow key={g.id} group={g} />)}
      </ul>
    </section>
  )
}
