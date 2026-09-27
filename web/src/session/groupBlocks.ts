import type { Block } from './reducer'
import { isArtifactBlock } from './artifacts'

/** A completed-or-pending tool block (tool is complete iff `result` is defined). */
export type ToolBlock = Extract<Block, { kind: 'tool' }>

/** Global override for how much tool detail to show in the Agent View. */
export type ToolDetailMode = 'auto' | 'all-open' | 'all-closed'

/** A turn's blocks, with consecutive tool blocks coalesced into groups. */
export type RenderItem =
  | { kind: 'block'; block: Block }
  | { kind: 'toolGroup'; tools: ToolBlock[] }
  // A `share_artifact` call: a tool block by wire shape, but never a collapsible
  // tool row — it is the deliverable the session wants seen (ArtifactCard).
  | { kind: 'artifact'; block: ToolBlock }

/**
 * Coalesce maximal runs of consecutive `kind:'tool'` blocks into `toolGroup`
 * items; all other blocks pass through as `block` items. Pure, order-preserving.
 * A `share_artifact` tool block breaks the run and becomes its own `artifact` item.
 */
export function groupBlocks(blocks: Block[]): RenderItem[] {
  const items: RenderItem[] = []
  let run: ToolBlock[] = []

  const flush = () => {
    if (run.length > 0) {
      items.push({ kind: 'toolGroup', tools: run })
      run = []
    }
  }

  for (const block of blocks) {
    if (block.kind === 'tool') {
      if (isArtifactBlock(block)) {
        flush()
        items.push({ kind: 'artifact', block })
      } else {
        run.push(block)
      }
    } else {
      flush()
      items.push({ kind: 'block', block })
    }
  }
  flush()
  return items
}
