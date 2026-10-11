import { useState } from 'react'
import { ApiRequestError } from '@/api/client'
import { useRevokePermissionRule } from '@/api/hooks'
import { useConfirm } from '@/ui/useConfirm'

export interface ErrorDetail { message?: string; reasons?: string[] }

export function detailOf(err: unknown): ErrorDetail {
  const body = err instanceof ApiRequestError ? err.body : null
  const d = body?.detail
  return typeof d === 'object' && d !== null ? (d as ErrorDetail) : {}
}

/** Confirm-then-revoke for one standing rule, with the failure kept inline. */
export function useRevokeRule(ruleId: string) {
  const revoke = useRevokePermissionRule()
  const { confirm, dialog } = useConfirm()
  const [revokeError, setRevokeError] = useState<string | null>(null)
  const onRevoke = async () => {
    const ok = await confirm({
      title: 'Revoke this rule?',
      description: 'Revoke this rule? Matching requests will ask again.',
      confirmLabel: 'Revoke',
      tone: 'danger',
    })
    if (!ok) return
    setRevokeError(null)
    try {
      await revoke.mutateAsync(ruleId)
    } catch (err) {
      setRevokeError(detailOf(err).message ?? 'Could not revoke the rule.')
    }
  }
  return { onRevoke, revokeError, dialog }
}
