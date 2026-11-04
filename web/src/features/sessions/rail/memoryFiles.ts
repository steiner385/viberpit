/**
 * How the Context card labels a loaded memory file (D-13). The engine reports
 * each file with the CLI's own `type` (`User`, `Project`, `Local`, `Managed`,
 * `AutoMem`); most read as they are, and only the ones that do not are mapped.
 * The path is shown through `worktreeRelativePath`, the transcript's one
 * worktree-path rule.
 */

/** Only the CLI types that do not read as words. Anything else is shown as sent. */
const TYPE_LABEL: Readonly<Record<string, string>> = {
  AutoMem: 'Auto memory',
}

export function memoryTypeLabel(type: string): string {
  return TYPE_LABEL[type] ?? type
}
