/** Depth → left inset for a flattened tree row. One table, because both row
 *  cards (`TreeRowCard`, `InitiativeCard`) render at the same depths and two
 *  copies of the scale would drift the first time it changed. */
const INDENT = ['pl-0', 'pl-4', 'pl-8', 'pl-12', 'pl-16', 'pl-20'] as const

export function indentClass(depth: number): string {
  return INDENT[Math.min(Math.max(depth, 0), INDENT.length - 1)]
}
