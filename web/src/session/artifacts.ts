import { mcpToolStem } from '@/brand'
import { extractText } from '@/lib/toolContent'
import type { Block } from './reducer'

/**
 * `share_artifact` — a session's deliverable (a URL, a local path, an image) made an
 * always-visible transcript card instead of a collapsed tool row.
 *
 * The server keeps the call an ordinary `ToolCall` (both transcript builders render
 * it, live and on replay — see `TURN_SIGNAL_NAMES` in
 * server/sessions/tools/registry.py), so the card is built HERE from the call's
 * input plus, for an image, the served URL in the handler's result envelope. No new
 * wire event, no new DB table: whatever the CLI transcript replays, the card
 * survives.
 */

export type ToolBlock = Extract<Block, { kind: 'tool' }>
export type ArtifactKind = 'url' | 'path' | 'image'

export interface Artifact {
  kind: ArtifactKind
  /** The URL or absolute host path the session named. */
  value: string
  label: string
  note: string | null
  /** `pending` until the handler result lands; `error` when the handler refused it. */
  status: 'pending' | 'ok' | 'error'
  /** For an ingested image: the edge-served `/api/attachments/{id}` URL. */
  url: string | null
  error: string | null
}

export const SHARE_ARTIFACT_TOOL = 'share_artifact'

const KINDS: ReadonlySet<string> = new Set<ArtifactKind>(['url', 'path', 'image'])

/** The served-attachment prefix the card will load an image from — anything else in
 *  a result envelope (a model-authored string, a tampered replay) is ignored. */
const ATTACHMENT_URL_PREFIX = '/api/attachments/'

// Deliberately NOT a type predicate: `block is ToolBlock` would tell the compiler
// the negative branch holds no tool blocks at all, which is false (every other tool).
export function isArtifactBlock(block: Block): boolean {
  return block.kind === 'tool' && mcpToolStem(block.name) === SHARE_ARTIFACT_TOOL
}

function str(v: unknown): string {
  if (typeof v === 'string') return v
  return v == null ? '' : String(v)
}

/** Total function: a malformed input or result degrades to a path-shaped card, never a throw. */
export function artifactFromBlock(block: ToolBlock): Artifact {
  const input = block.input ?? {}
  const rawKind = str(input.kind)
  const kind = (KINDS.has(rawKind) ? rawKind : 'path') as ArtifactKind
  const base = { kind, value: str(input.value), label: str(input.label), note: str(input.note) || null }
  const result = block.result
  if (!result) return { ...base, status: 'pending', url: null, error: null }
  const payload = result.payload as { content?: unknown } | undefined
  const text = (extractText(payload?.content) ?? '').trim()
  if (!result.ok) return { ...base, status: 'error', url: null, error: text || 'share_artifact failed' }
  return { ...base, status: 'ok', url: servedUrl(text), error: null }
}

function servedUrl(text: string): string | null {
  try {
    const env = JSON.parse(text) as { data?: { url?: unknown } }
    const url = env?.data?.url
    return typeof url === 'string' && url.startsWith(ATTACHMENT_URL_PREFIX) ? url : null
  } catch {
    return null
  }
}
