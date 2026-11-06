/**
 * A discard's recovery refs and the command that restores the first — one rendering
 * for the discard dialog / toast and the archived reader, so the two cannot drift.
 */
import { CopyablePath } from '../transcript/CopyablePath'

/** A ref or command as copyable monospace text that wraps rather than widening the dialog. */
export function Code({ text }: { text: string }) {
  return <code className="block min-w-0 break-all rounded bg-surface-raised px-1.5 py-0.5 font-mono text-xs text-ink">{text}</code>
}

/** The restore command, copied by a click or Enter (the worktree path's own control). */
export function Restore({ command }: { command: string }) {
  return (
    <span className="block min-w-0 break-all font-mono text-xs" data-testid="restore-command">
      <CopyablePath text={command} title="Copy restore command" />
      <span className="ml-1 font-sans text-ink-muted">(click to copy)</span>
    </span>
  )
}

export function RecoveryRefs({ refs, restore }: { refs: readonly string[]; restore: string | null }) {
  return (
    <div className="space-y-1 min-w-0" data-testid="discard-recovery">
      {refs.length > 0 && <p className="text-xs text-ink-muted">Saved as {refs.length === 1 ? 'a recovery ref' : 'recovery refs'}:</p>}
      {refs.map((r) => <Code key={r} text={r} />)}
      {restore && (
        <>
          <p className="text-xs text-ink-muted">Restore with:</p>
          <Restore command={restore} />
        </>
      )}
    </div>
  )
}

