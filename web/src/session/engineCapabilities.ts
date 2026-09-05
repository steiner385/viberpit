/**
 * Whether a capability-gated control renders.
 *
 * A pure function, deliberately NOT in `@/api/hooks`: ~50 suites replace that
 * module with a hand-written `vi.mock` factory, so anything exported from it has
 * to be restated in every one of them. A pure helper here is imported for real
 * by mocked and unmocked tests alike.
 *
 * An UNKNOWN capability counts as PRESENT. The engine report is advisory for the
 * UI — the server refuses an unsupported action regardless — so a slow or old
 * `/api/config` must degrade to today's behaviour rather than silently hiding
 * controls that work.
 */
import type { AgentCapabilities } from '@/api/types'

export function canUseEngine(
  caps: AgentCapabilities | undefined,
  key: keyof AgentCapabilities,
): boolean {
  return caps?.[key] !== false
}
