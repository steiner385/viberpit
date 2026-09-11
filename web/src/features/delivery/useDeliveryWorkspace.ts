/**
 * The Delivery section's workspace: list props + navigation, the sibling of
 * `useRoadmapWorkspace` and `AppShell`'s `useSessionsWorkspace`. Selection is
 * the URL (`deliveryNavState`).
 *
 * @param active whether the Delivery list column is the one the shell is about
 *   to render. The hook is called unconditionally (hooks always are), so this
 *   is what keeps `/pipeline/repos` off the four other sections and off kiosk,
 *   where `app.list` does not exist at all.
 */
import { useCallback, useMemo } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import { useAutomations, useStageModels } from '@/api/hooks'
import type { DeliveryListColumnProps } from './DeliveryListColumn'
import {
  automationPath, ciRepoPath, deliveryOpenItem, deliveryScope, keyForDeliveryItem,
} from './deliveryNavState'

export function useDeliveryWorkspace(active: boolean): { list: DeliveryListColumnProps } {
  const navigate = useNavigate()
  const { pathname, search } = useLocation()
  const params = useParams()
  // Ungated on purpose, unlike the repo set below: `QK.automations` is the
  // Delivery NAV BADGE's key (`useNavBadges`), polled app-wide so the badge is
  // right while you are looking at another section. Gating it here would not
  // save a request — it would only make the column a second observer of a key
  // that is already live, which is what `useAutomations` has no `enabled`
  // parameter to express anyway.
  const automations = useAutomations()
  // `enabled: active` is the gate — nothing else keeps the 30 s repo poll off
  // the four sections that never render this column. `poll: false` is the
  // second half: `DeliveryPage`'s CI welcome observes this exact cache entry,
  // and two polling observers of one key are two requests.
  const repos = useStageModels({ enabled: active, poll: false })

  const onOpenAutomation = useCallback((id: string) => navigate(automationPath(id)), [navigate])
  const onOpenRepo = useCallback((repo: string) => navigate(ciRepoPath(repo)), [navigate])

  const repoList = useMemo(() => Object.keys(repos.data ?? {}), [repos.data])

  return {
    list: {
      scope: deliveryScope(pathname, search),
      automations: automations.data ?? [],
      automationsLoading: automations.isLoading,
      automationsError: automations.isError,
      repos: repoList,
      reposLoading: repos.isLoading,
      reposError: repos.isError,
      selectedKey: keyForDeliveryItem(deliveryOpenItem(params, new URLSearchParams(search))),
      onOpenAutomation,
      onOpenRepo,
    },
  }
}
