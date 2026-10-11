import { toast } from 'sonner'

/** The toast for a TriggerResult-shaped outcome: one level → tone mapping for
 *  every automation action (card buttons and the clustered inbox alike). */
export function outcomeToast(r: { level: string; message: string }) {
  if (r.level === 'ok') toast.success(r.message)
  else if (r.level === 'warn') toast.warning(r.message)
  else toast.error(r.message)
}
