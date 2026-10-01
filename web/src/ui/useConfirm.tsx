import * as React from 'react'
import { ConfirmDialog, type ConfirmDialogProps } from './ConfirmDialog'

export type ConfirmRequest = Pick<ConfirmDialogProps, 'title' | 'description' | 'confirmLabel' | 'tone' | 'typed'>

/**
 * `ConfirmDialog` as a promise: `const ok = await confirm({ title, … })`.
 *
 * One gate for every destructive or fleet-wide action in Settings. Before this,
 * the same question was asked four different ways — the Typed tier for an
 * archive move, a `ConfirmDialog` for an unsafe permission mode, `window.confirm`
 * for a snapshot restore, and nothing at all for removing a host or deleting a
 * channel, rule or hook (2026-09-30 Settings critique, P1). A caller awaits the
 * answer inline, so the mutation stays where it was and the policy (which tier,
 * what the sentence says) is the only thing that moves.
 *
 * Render `dialog` once in the caller's tree. The dialog nests under an open
 * `Dialog` safely: `useFocusTrap` stacks traps and lifts `inert` only when the
 * outermost one closes. Resolving `false` on close means an Escape, a backdrop
 * tap and Cancel all read as "no".
 */
export function useConfirm(): { confirm: (req: ConfirmRequest) => Promise<boolean>; dialog: React.ReactNode } {
  const [pending, setPending] = React.useState<{ req: ConfirmRequest; resolve: (ok: boolean) => void } | null>(null)
  const confirm = React.useCallback((req: ConfirmRequest) =>
    new Promise<boolean>((resolve) => setPending({ req, resolve })), [])
  const settle = (ok: boolean) => {
    pending?.resolve(ok)
    setPending(null)
  }
  const dialog = pending ? (
    <ConfirmDialog
      open
      onClose={() => settle(false)}
      onConfirm={() => settle(true)}
      title={pending.req.title}
      description={pending.req.description}
      confirmLabel={pending.req.confirmLabel}
      tone={pending.req.tone}
      typed={pending.req.typed}
    />
  ) : null
  return { confirm, dialog }
}
