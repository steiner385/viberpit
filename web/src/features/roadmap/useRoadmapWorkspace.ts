/**
 * The Roadmap section's workspace: list props + navigation, the sibling of
 * `AppShell`'s `useSessionsWorkspace`. Selection is the URL (`roadmapNavState`).
 *
 * @param active whether the Roadmap list column is the one the shell is about to
 *   render. The hook is called unconditionally (hooks always are), so this is
 *   what keeps the map's 15 s poll off the six other sections and off kiosk,
 *   where `app.list` does not exist at all.
 */
import { useCallback } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import { useRoadmapMap, useTriage } from '@/api/hooks'
import type { TriageProposal } from '@/api/triage'
import type { RoadmapListColumnProps } from './RoadmapListColumn'
import {
  capabilityPath, inboxPath, inboxProductForSearch, initiativePath, keyForOpenItem, productPath,
  roadmapOpenItem, roadmapScopeForSearch,
} from './roadmapNavState'
import type { TreeRow } from './treeRows'

export function useRoadmapWorkspace(active: boolean): { list: RoadmapListColumnProps } {
  const navigate = useNavigate()
  const { search } = useLocation()
  const params = useParams()
  const { data: map, isLoading, isError } = useRoadmapMap({ enabled: active })
  // The `?product=` chip, and with it the cache key the optimistic accept /
  // reject / bulk mutations write back through — they must patch the SAME
  // `useTriage` key the rows came from or the row does not leave the list.
  const product = inboxProductForSearch(search)
  // Gated on the SECTION, not the scope: the queue depth badges the Inbox link
  // from Map and Initiatives too, so it is fetched in every scope — but the
  // column exists only while Roadmap is the section, and `enabled` is the only
  // thing that keeps this 15 s poll off the six sections that never show it.
  const triage = useTriage(product ?? undefined, { enabled: active })

  const onOpen = useCallback((row: TreeRow) => {
    if (row.kind === 'product') navigate(productPath(row.product.slug))
    else if (row.kind === 'capability') navigate(capabilityPath(row.product.slug, row.capability.slug))
    else if (row.kind === 'initiative') navigate(initiativePath(row.initiative.slug))
  }, [navigate])

  // The map's awaiting-triage pseudo row: same section, Inbox scope, narrowed
  // to that product.
  const onOpenInbox = useCallback((productSlug: string) => {
    navigate(inboxPath({ product: productSlug }))
  }, [navigate])

  const onOpenProposal = useCallback((p: TriageProposal, opts?: { edit: boolean }) => {
    // The editor IS the detail pane (spec §12), so `e` is "open with the score
    // fields focused", not a second inline form.
    navigate(inboxPath({ product, proposal: p.id }) + (opts?.edit ? '&edit=1' : ''))
  }, [navigate, product])

  const onClearProduct = useCallback(() => navigate(inboxPath({})), [navigate])

  return {
    list: {
      scope: roadmapScopeForSearch(search),
      map,
      isLoading,
      isError,
      selectedKey: keyForOpenItem(roadmapOpenItem(params, new URLSearchParams(search))),
      onOpen,
      onOpenInbox,
      inbox: {
        proposals: triage.data ?? [],
        isLoading: triage.isLoading,
        isError: triage.isError,
        product,
        onOpenProposal,
        onClearProduct,
      },
    },
  }
}
