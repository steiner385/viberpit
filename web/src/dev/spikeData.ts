// SPIKE-only synthetic transcript generator. Produces turns with the same Block
// shapes the real reducer emits, with deliberately varied heights (short user
// bubbles, long markdown, collapsible tool groups, thinking) to stress the
// virtualizer's dynamic measurement.
import type { Turn as TurnType, Block } from '@/session/reducer'
import type { ToolKind } from '@/session/events'

const LOREM =
  'The quick brown fox jumps over the lazy dog. Pack my box with five dozen liquor jugs. ' +
  'How vexingly quick daft zebras jump! Sphinx of black quartz, judge my vow.'

function pick<T>(arr: T[], i: number): T {
  return arr[i % arr.length]
}

function paragraph(seed: number): string {
  const sentences = 1 + (seed % 6)
  return Array.from({ length: sentences }, () => LOREM).join(' ')
}

function toolBlock(turnId: string, i: number): Block {
  const kinds: ToolKind[] = ['read', 'bash', 'edit', 'search']
  const kind = pick(kinds, i)
  const id = `${turnId}-tool-${i}`
  const input =
    kind === 'bash'
      ? { command: `grep -rn "thing-${i}" src/` }
      : kind === 'edit'
        ? { file_path: `src/file${i}.ts`, old_string: `old line ${i}\nmore`, new_string: `new line ${i}\nmore\nextra` }
        : { file_path: `src/file${i}.ts` }
  const payload =
    kind === 'bash'
      ? { content: Array.from({ length: 5 + (i % 30) }, (_, n) => `out line ${n}`).join('\n') }
      : kind === 'read'
        ? { content: Array.from({ length: 10 + (i % 50) }, (_, n) => `const x${n} = ${n}`).join('\n') }
        : { content: 'The file has been updated.' }
  return { kind: 'tool', id, toolId: id, name: kind === 'bash' ? 'Bash' : kind === 'edit' ? 'Edit' : kind === 'read' ? 'Read' : 'Grep', toolKind: kind, input, result: { ok: i % 9 !== 0, payload } }
}

export function makeAssistantTurn(n: number): TurnType {
  const id = `t-${n}`
  const blocks: Block[] = []
  if (n % 3 === 0) blocks.push({ kind: 'thinking', id: `${id}-think`, text: paragraph(n + 2) })
  blocks.push({ kind: 'text', id: `${id}-text0`, text: `**Step ${n}.** ${paragraph(n)}\n\n- point a\n- point b` })
  const toolCount = 1 + (n % 6)
  for (let i = 0; i < toolCount; i++) blocks.push(toolBlock(id, n * 10 + i))
  if (n % 2 === 0) blocks.push({ kind: 'text', id: `${id}-text1`, text: paragraph(n + 1) })
  return { id, role: 'assistant', blocks }
}

export function makeUserTurn(n: number): TurnType {
  return { id: `u-${n}`, role: 'user', blocks: [{ kind: 'text', id: `u-${n}-t`, text: `Question ${n}: please do the thing.` }] }
}

/** Build an alternating user/assistant transcript of `count` turns. */
export function makeTranscript(count: number): TurnType[] {
  const turns: TurnType[] = []
  for (let n = 0; n < count; n++) {
    turns.push(n % 2 === 0 ? makeUserTurn(n) : makeAssistantTurn(n))
  }
  return turns
}
