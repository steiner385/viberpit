import type { Block } from './reducer'
import { isArtifactBlock } from './artifacts'
import { userFacingToolName } from './userFacingTools'

/** A completed-or-pending tool block (tool is complete iff `result` is defined). */
export type ToolBlock = Extract<Block, { kind: 'tool' }>

/** Global override for how much tool detail to show in the Agent View. */
export type ToolDetailMode = 'auto' | 'all-open' | 'all-closed'

/** A turn's blocks, with consecutive tool blocks coalesced into groups. */
export type RenderItem =
  | { kind: 'block'; block: Block }
  | { kind: 'toolGroup'; tools: ToolBlock[] }
  // A tool call whose purpose is to show the user something — `share_artifact`
  // (ArtifactCard) or a CLI user-facing tool (UserFacingToolCard): a tool block by
  // wire shape, but never a collapsible tool row.
  | { kind: 'artifact'; block: ToolBlock }

/**
 * Coalesce maximal runs of consecutive `kind:'tool'` blocks into `toolGroup`
 * items; all other blocks pass through as `block` items. Pure, order-preserving.
 * A `share_artifact` or user-facing tool block (`SendUserFile`, …) breaks the run
 * and becomes its own `artifact` item. Sub-agent calls never reach here for the
 * main thread — the reducer routes them into their Task's sub-thread.
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
      if (isArtifactBlock(block) || userFacingToolName(block) !== null) {
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
