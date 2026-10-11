import { useState } from 'react'
import { apiErrorDetail } from '@/api/client'
import { useRevokePermissionRule } from '@/api/hooks'
import { useConfirm } from '@/ui/useConfirm'

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
      setRevokeError(apiErrorDetail(err).message ?? 'Could not revoke the rule.')
    }
  }
  return { onRevoke, revokeError, dialog }
}
