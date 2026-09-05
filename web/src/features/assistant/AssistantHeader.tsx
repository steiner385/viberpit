/**
 * AssistantHeader — the AI drawer's top row: which model the assistant runs on
 * (switchable), and a "New chat" reset.
 *
 * The picker posts to the ordinary session model route, because the assistant IS a
 * session. It does NOT reuse the composer's model menu (`SessionMeta` in
 * `SessionBody.tsx`): that one is wired to the full session status strip and carries
 * effort, queueing and approval affordances the drawer has no room for.
 */
import { useState } from 'react'
import { useAssistantStatus, useResetAssistant, useSetAssistantModel } from '@/api/assistant'
import { useModels } from '@/api/hooks'
import { apiErrorCode } from '@/api/client'
import { modelName } from '@/session/modelName'
import { Dialog } from '@/ui/Dialog'
import { Button } from '@/ui/Button'
import { Select } from '@/ui/Select'

export function AssistantHeader() {
  const status = useAssistantStatus()
  const { data: models } = useModels()
  const reset = useResetAssistant()
  const setModel = useSetAssistantModel()
  const [confirming, setConfirming] = useState(false)

  const sessionId = status.data?.sessionId ?? null
  const modelId = status.data?.model ?? null

  // Until a session exists there is nothing to switch, and the status query is the
  // only thing that names the current model — so fall back to a read-only label.
  const modelLabel = models?.find((m) => m.id === modelId)?.label
    ?? (modelId ? modelName(modelId) || modelId : '—')

  return (
    <div className="flex items-center justify-between gap-2 border-b border-edge px-3 py-2">
      <div className="min-w-0">
        <p className="truncate text-sm font-medium text-ink">Assistant</p>
        {sessionId ? (
          <Select
            id="assistant-model"
            aria-label="Assistant model"
            size="sm"
            className="mt-0.5 w-auto max-w-full"
            value={modelId ?? ''}
            disabled={setModel.isPending}
            onChange={(e) => {
              void setModel.mutateAsync({ sessionId, model: e.target.value })
            }}
          >
            {(models ?? []).map((m) => (
              <option key={m.id} value={m.id}>{m.label}</option>
            ))}
          </Select>
        ) : (
          <p className="truncate text-xs text-ink-muted">{modelLabel}</p>
        )}
        {setModel.isError && (
          <p role="alert" className="text-xs text-danger">
            {apiErrorCode(setModel.error) === 'MODEL_GATED'
              ? 'That model needs approval and can’t run the assistant.'
              : 'Couldn’t switch model.'}
          </p>
        )}
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
