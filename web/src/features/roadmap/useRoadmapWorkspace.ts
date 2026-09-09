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
import { useRoadmapMap } from '@/api/hooks'
import { NAV_BY_ID } from '@/shell/navDefs'
import type { RoadmapListColumnProps } from './RoadmapListColumn'
import {
  capabilityPath, initiativePath, keyForOpenItem, productPath, roadmapOpenItem,
  roadmapScopeForSearch,
} from './roadmapNavState'
import type { TreeRow } from './treeRows'

export function useRoadmapWorkspace(active: boolean): { list: RoadmapListColumnProps } {
  const navigate = useNavigate()
  const { search } = useLocation()
  const params = useParams()
  const { data: map, isLoading, isError } = useRoadmapMap({ enabled: active })

  const onOpen = useCallback((row: TreeRow) => {
    if (row.kind === 'product') navigate(productPath(row.product.slug))
    else if (row.kind === 'capability') navigate(capabilityPath(row.product.slug, row.capability.slug))
    else if (row.kind === 'initiative') navigate(initiativePath(row.initiative.slug))
  }, [navigate])

  // The inbox is still the Triage section until finish-line row 3 folds it in.
  const onOpenInbox = useCallback((productSlug: string) => {
    navigate(`${NAV_BY_ID.triage.path}?projectSlug=${encodeURIComponent(productSlug)}`)
  }, [navigate])

  return {
    list: {
      scope: roadmapScopeForSearch(search),
      map,
      isLoading,
      isError,
      selectedKey: keyForOpenItem(roadmapOpenItem(params, new URLSearchParams(search))),
      onOpen,
      onOpenInbox,
    },
  }
}
