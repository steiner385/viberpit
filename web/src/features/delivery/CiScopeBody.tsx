// The CI health scope of the Delivery list column. Same frame as
// `AutomationsScopeBody` (`ScopeBody` + `ListSearchInput`, no keys, no bulk
// bar), sorted by repo name rather than tier — CI health carries no tier of
// its own, just each repo's 7-day check meta line (`CiRepoRow`).
import { useMemo, useState } from 'react'
import { ListSearchInput } from '@/ui/list/ListSearchInput'
import { Banner } from '@/ui/Banner'
import { EmptyState } from '@/ui/EmptyState'
import { Skeleton } from '@/ui/Skeleton'
import { DELIVERY } from '@/features/products/vocab'
import { ScopeBody } from '@/features/sessions/ScopeBody'
import { ciRepoKey } from './deliveryNavState'
import { CiRepoRow } from './rows/CiRepoRow'

export interface CiScopeBodyProps {
  repos: readonly string[]
  isLoading: boolean
  isError: boolean
  selectedKey: string | null
  onOpen: (repo: string) => void
}

export function CiScopeBody({ repos, isLoading, isError, selectedKey, onOpen }: CiScopeBodyProps) {
  const [query, setQuery] = useState('')
  const q = query.trim().toLowerCase()
  const visible = useMemo(
    () => (q ? repos.filter((r) => r.toLowerCase().includes(q)) : repos),
    [repos, q],
  )
  const sorted = useMemo(() => [...visible].sort((a, b) => a.localeCompare(b)), [visible])

  return (
    <ScopeBody
      controls={
        <ListSearchInput
          value={query}
          onChange={setQuery}
          placeholder={`Search ${DELIVERY.ci.toLowerCase()}…`}
          ariaLabel={`Search ${DELIVERY.ci.toLowerCase()}`}
        />
      }
    >
      {isError && <Banner tone="danger" title={DELIVERY.ciError} />}
      {isLoading && repos.length === 0 && (
        <div role="status" aria-label={`Loading ${DELIVERY.ci.toLowerCase()}`} className="space-y-2">
          {Array.from({ length: 3 }, (_, i) => (
            <div key={i} data-testid="skeleton"><Skeleton lines={1} /></div>
          ))}
        </div>
      )}
      {!isLoading && sorted.length === 0 && (
        <EmptyState title={q ? 'No matches' : DELIVERY.emptyRepos} />
      )}
      {sorted.length > 0 && (
        <div role="list" aria-label={DELIVERY.ci} className="flex flex-col gap-1">
          {sorted.map((repo) => (
            <CiRepoRow
              key={repo}
              repo={repo}
              selected={selectedKey === ciRepoKey(repo)}
              onOpen={onOpen}
            />
          ))}
        </div>
      )}
    </ScopeBody>
  )
}
