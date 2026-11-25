import { lazy, Suspense, type ComponentType } from 'react'
import type { ToolBlock } from '@/session/artifacts'
import { userFacingDisplayName, userFacingToolSummary, userFacingView, type UserFacingToolName, type UserFacingView } from '@/session/userFacingTools'

interface Props {
  block: ToolBlock
  name: UserFacingToolName
}

/** The one-line stand-in while the card chunk loads — or for good, if it cannot
 *  (stale hashes after a release): never a bare, empty tool row. */
function Fallback({ block, name }: Props) {
  const summary = userFacingToolSummary(name, block.input ?? {})
  return (
    <p className="my-2 text-sm text-ink-muted" data-testid="user-facing-tool-fallback">
      <span className="font-medium text-ink">{userFacingDisplayName(name)}</span>{summary && `: ${summary}`}
    </p>
  )
}

// The card bodies (Files links, image reads, markdown) stay off the initial
// payload: Turn is in the eager transcript tree, and `npm run budget` gates it.
// They take a pre-parsed `UserFacingView`, never an eager module's values — see
// `userFacingView`. A failed chunk load keeps the one-line fallback.
const Body = lazy<ComponentType<Props & { view: UserFacingView }>>(() => import('./UserFacingToolBody').then(
  (m) => ({ default: m.UserFacingToolBody }),
  () => ({ default: Fallback }),
))

/**
 * A CLI user-facing tool call (`SendUserFile`, `SendUserMessage`,
 * `ReportFindings`, `PushNotification`) shown as what it is — the thing the
 * session meant the user to see — instead of a collapsed tool row.
 */
export function UserFacingToolCard({ block, name }: Props) {
  return (
    <Suspense fallback={<Fallback block={block} name={name} />}>
      <Body view={userFacingView(name, block)} block={block} name={name} />
    </Suspense>
  )
}
