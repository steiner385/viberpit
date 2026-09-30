/**
 * The Roadmap section's workspace: list props + navigation, the sibling of
 * `AppShell`'s `useSessionsWorkspace`. Selection is the URL (`roadmapNavState`).
 *
 * @param active whether the Roadmap list column is the one the shell is about to
 *   render. The hook is called unconditionally (hooks always are), so this is
 *   what keeps the map's 15 s poll off the four other sections and off kiosk,
 *   where `app.list` does not exist at all.
 */
import { useCallback, useMemo } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import { useDecisions, useRoadmapMap, useTriage } from '@/api/hooks'
import type { InboxProposal } from '@/api/triage'
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
  // column exists only while Roadmap is the section. Unlike `/pm/map`, the
  // unscoped queue IS polled app-wide by design (the Roadmap nav badge,
  // `useNavBadges`), so `enabled` is not what keeps it off the five other
  // sections — it is what keeps the column from being a SECOND observer of a
  // key that is already live, and the only thing that keeps any `/pm/triage`
  // off kiosk, which renders no nav badges at all.
  //
  // `poll` for the same reason, one level down: the unscoped key rides the
  // badge's 15 s interval, so the column adds no timer to it; a `?product=`
  // chip is its own key with no other observer, so that one polls itself.
  const triage = useTriage(product ?? undefined, { enabled: active, poll: product !== null })
  // The inbox's second card kind (spec 2026-09-18 §6), scoped server-side like
  // triage and asked for its downstream impact. The impact key is its own entry
  // (the badge's bare list has none), so it polls whenever a product chip keys it.
  const decisions = useDecisions(product ?? undefined, { enabled: active, poll: product !== null, impact: true })
  // The rows are concatenated into ONE queue — the ordering spec, the key
  // map and the pending count all act on the whole inbox, not on two halves.
  const proposals = useMemo<InboxProposal[]>(() => [
    ...(triage.data ?? []),
    ...(decisions.data ?? []),
  ], [triage.data, decisions.data])

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

  const onOpenProposal = useCallback((p: InboxProposal, opts?: { edit: boolean }) => {
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
        proposals,
        // Either list failing is the inbox failing: a banner that stayed quiet
        // because only the decisions query broke would show a short queue as if
        // it were the whole one.
        isLoading: triage.isLoading || decisions.isLoading,
        isError: triage.isError || decisions.isError,
        product,
        onOpenProposal,
        onClearProduct,
      },
    },
  }
}
