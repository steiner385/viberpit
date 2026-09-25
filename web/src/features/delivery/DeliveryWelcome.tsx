/**
 * The Delivery page with nothing open: "there is a list, pick something from
 * it", or "the list is empty". One component for both scopes, because the pane
 * says the same kind of thing either way and the scope only changes the noun.
 *
 * The sibling of `roadmap/inbox/InboxWelcome` — the pane always says something,
 * because on desktop it is half the screen and on phone it is the whole of it
 * the moment a row is opened and then closed again.
 */
import { DELIVERY } from '@/features/products/vocab'
import { EmptyState } from '@/ui/EmptyState'
import { AssistantGapsTable } from './ci/AssistantGapsTable'
import type { DeliveryScope } from './deliveryNavState'

export interface DeliveryWelcomeProps {
  scope: DeliveryScope
  /** Rows in this scope's list. */
  count: number
  /** The list fetch failed: `count` is not really zero, it is unknown — the
   *  "nothing registered" sentence would be a false claim. */
  isError?: boolean
}

export function DeliveryWelcome({ scope, count, isError }: DeliveryWelcomeProps) {
  if (scope === 'ci') {
    // The assistant-gaps table (the old Insights page's other readout) does not
    // depend on the repo list, so it shows under every sentence — even a failed one.
    return (
      <div className="space-y-4">
        {isError ? (
          <EmptyState
            title="No repositories to show"
            description="It will fill in as soon as the list loads again."
          />
        ) : count === 0 ? (
          <EmptyState title={DELIVERY.emptyRepos} />
        ) : (
          <EmptyState
            title="Pick a repository"
            description={`${count} with pipeline history — open one to see its slowest, flakiest and regressing checks.`}
          />
        )}
        <AssistantGapsTable />
      </div>
    )
  }
  if (isError) {
    return (
      <EmptyState
        title="No automations to show"
        description="It will fill in as soon as the list loads again."
      />
    )
  }
  if (count === 0) {
    return <EmptyState title={DELIVERY.emptyAutomations} />
  }
  return (
    <EmptyState
      title="Pick an automation"
      description={`${count} registered — open one to see what it is doing, what it needs from you, and how it is configured.`}
    />
  )
}
