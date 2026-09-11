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
  // Delivery NAV BADGE's key (`useNavBadges`), read on every route so the badge
  // is right while you are looking at another section. Gating it here would not
  // save a request — the column is an OBSERVER of a key that is already live,
  // not a second fetch of it, which is why `useAutomations` has no `enabled`
  // parameter to express the difference. If that key ever earns a
  // `refetchInterval`, it belongs on ONE observer (the badge's), never on both.
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
      selectedKey: keyForDeliveryItem(deliveryOpenItem(params)),
      onOpenAutomation,
      onOpenRepo,
    },
  }
}
