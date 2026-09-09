/**
 * The Inbox scope's detail pane with nothing open: either "there is a queue,
 * pick something from it" or "there is no queue".
 *
 * The sibling of the Map scope's portfolio — the pane always says something,
 * because on desktop it is half the screen and on phone it is the whole of it
 * the moment a row is opened and then closed again.
 */
import { EmptyState } from '@/ui/EmptyState'

export interface InboxWelcomeProps {
  /** Proposals waiting in the current scope. */
  count: number
  /** The `?product=` narrowing, or null for every product. */
  product: string | null
}

export function InboxWelcome({ count, product }: InboxWelcomeProps) {
  const scope = product ? ` in ${product}` : ''
  if (count === 0) {
    return (
      <EmptyState
        title="Inbox zero"
        description={`Nothing is waiting to be triaged${scope}.`}
      />
    )
  }
  return (
    <EmptyState
      title="Pick a proposal"
      description={`${count} ${count === 1 ? 'proposal is' : 'proposals are'} waiting${scope}. Open one to see what the classifier proposed, and accept, edit or reject it.`}
    />
  )
}
