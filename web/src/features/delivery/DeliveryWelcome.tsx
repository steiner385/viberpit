/**
 * The Delivery page with nothing open: "there is a list, pick something from
 * it", or "the list is empty". One component for both scopes, because the pane
 * says the same kind of thing either way and the scope only changes the noun.
 *
 * The sibling of `roadmap/inbox/InboxWelcome` — the pane always says something,
 * because on desktop it is half the screen and on phone it is the whole of it
 * the moment a row is opened and then closed again.
 */
import { EmptyState } from '@/ui/EmptyState'
import type { DeliveryScope } from './deliveryNavState'

export interface DeliveryWelcomeProps {
  scope: DeliveryScope
  /** Rows in this scope's list. */
  count: number
}

export function DeliveryWelcome({ scope, count }: DeliveryWelcomeProps) {
  if (scope === 'ci') {
    if (count === 0) {
      return <EmptyState title="No repositories with pipeline history yet" />
    }
    return (
      <EmptyState
        title="Pick a repository"
        description={`${count} with pipeline history — open one to see its slowest, flakiest and regressing checks.`}
      />
    )
  }
  if (count === 0) {
    return <EmptyState title="No automations registered" />
  }
  return (
    <EmptyState
      title="Pick an automation"
      description={`${count} registered — open one to see what it is doing, what it needs from you, and how it is configured.`}
    />
  )
}
