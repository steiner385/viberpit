/**
 * AssistantHeader — the AI drawer's top row: which model the assistant is running on,
 * and a "New chat" reset.
 *
 * Deliberately minimal: there is no model-CHANGE endpoint for the assistant session
 * today (`@/api/assistant` exposes status + create/resume/reset only), and the
 * composer's own model menu (`SessionMeta` in `SessionBody.tsx`) is wired tightly to
 * the full session status strip — not reasonably extractable for this. So this reads
 * the current model and displays it; picking a different one is a later task once a
 * real endpoint exists, not a fake mutation bolted on here.
 */
import { useState } from 'react'
import { useAssistantStatus, useResetAssistant } from '@/api/assistant'
import { useModels } from '@/api/hooks'
import { modelName } from '@/session/modelName'
import { Dialog } from '@/ui/Dialog'
import { Button } from '@/ui/Button'

export function AssistantHeader() {
  const status = useAssistantStatus()
  const { data: models } = useModels()
  const reset = useResetAssistant()
  const [confirming, setConfirming] = useState(false)

  const modelId = status.data?.model ?? null
  const modelLabel = models?.find((m) => m.id === modelId)?.label
    ?? (modelId ? modelName(modelId) || modelId : '—')

  return (
    <div className="flex items-center justify-between gap-2 border-b border-edge px-3 py-2">
      <div className="min-w-0">
        <p className="truncate text-sm font-medium text-ink">Assistant</p>
        <p className="truncate text-xs text-ink-muted">{modelLabel}</p>
      </div>
      <Button type="button" variant="secondary" size="sm" onClick={() => setConfirming(true)}>
        New chat
      </Button>

      {confirming && (
        <Dialog open onClose={() => setConfirming(false)} title="Start a new chat?">
          <p className="text-sm text-ink-muted">
            This clears the assistant&rsquo;s current conversation and starts a fresh session.
          </p>
          <div className="flex justify-end gap-3 pt-2">
            <Button type="button" variant="secondary" size="lg" onClick={() => setConfirming(false)}>
              Cancel
            </Button>
            <Button
              type="button"
              variant="primary"
              size="lg"
              onClick={() => { void reset.mutateAsync({}).then(() => setConfirming(false)) }}
            >
              Start new chat
            </Button>
          </div>
        </Dialog>
      )}
    </div>
  )
}
