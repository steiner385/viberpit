import { useCallback, useState } from 'react'
import type { UseMutationResult } from '@tanstack/react-query'
import type { ApiRequestError } from '@/api/client'
import type { PluginOpResult } from '@/api/types'
import type { OpRecord } from './HostOutcomes'
import { refusalMessage } from './pluginText'

/**
 * Runs a fanned-out plugin mutation and keeps its last per-host outcome (or refusal)
 * for display, with a Retry that re-sends the same variables. Kept outside the mutation
 * object so a later `reset()` (the toggle's optimistic state) never wipes the outcome.
 */
export function useOpRecord<V, R extends PluginOpResult>(
  mutation: UseMutationResult<R, ApiRequestError, V>,
) {
  const [record, setRecord] = useState<OpRecord | null>(null)
  const [error, setError] = useState<string | null>(null)
  const { mutate } = mutation
  const run = useCallback(function go(vars: V, label: string, opts: { note?: string; onSuccess?: (r: R) => void; onError?: (e: unknown) => void } = {}) {
    setError(null)
    mutate(vars, {
      onSuccess: (result) => {
        setRecord({ label, result, note: opts.note, retry: () => go(vars, label, opts) })
        opts.onSuccess?.(result)
      },
      onError: (e) => {
        setRecord(null)
        setError(`${label}: ${refusalMessage(e)}`)
        opts.onError?.(e)
      },
    })
  }, [mutate])
  const clear = useCallback(() => { setRecord(null); setError(null) }, [])
  return { run, record, error, clear, pending: mutation.isPending }
}
