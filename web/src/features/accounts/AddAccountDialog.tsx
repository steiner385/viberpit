/**
 * AddAccountDialog — collects a name for a new account, then drives either:
 *   - Anthropic: the claude.ai sign-in as a code paste in the operator's own browser
 *     (the same state machine as the re-auth paste, `claude_login`), which creates
 *     the account's credential slot once the pasted code is exchanged.
 *   - GitHub Copilot: the device-flow OAuth to create a Copilot account slot.
 *   - OpenAI (ChatGPT): the SAME device flow, at the credential authority, to
 *     create an OpenAI account the Codex engine can launch.
 *
 * The two device flows share `DeviceCodePanel` rather than each carrying their
 * own copy of the code/link/failed markup — they are one state machine on the
 * wire, and the backend spells them identically on purpose.
 *
 * Single source of truth for the add-account flow: imported by both the mobile
 * Dashboard accounts tab and the desktop AccountsManager pane. All add-account
 * hooks live here so they are only invoked while the dialog is open — keeping
 * them out of the always-mounted account panels.
 */
import * as React from 'react'
import { useState, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { cn } from '@/lib/utils'
import { Dialog } from '@/ui/Dialog'
import { Button } from '@/ui/Button'
import { Field } from '@/ui/Field'
import { Input } from '@/ui/Input'
import {
  useAddAccount, useAddAccountStatus, useCancelAddAccount,
  useAddCopilotAccount, useCopilotAddStatus, useCancelCopilotAdd,
  useAddOpenAIAccount, useOpenAIAddStatus, useCancelOpenAIAdd,
  QK,
} from '@/api/hooks'
import { accountNameError } from '@/lib/accountName'
import { CodePastePanel } from './CodePastePanel'
import { useSubmitAddAccountCode } from './useAddAccountCode'
import { DeviceCodePanel } from './DeviceCodePanel'

/** The providers this dialog can enrol. Ordered as the toggle renders them. */
type AddProvider = 'anthropic' | 'copilot' | 'openai'

/** Where the user goes to type the device code. */
const DEVICE_PROVIDER_LABEL: Record<'copilot' | 'openai', string> = {
  copilot: 'GitHub',
  openai: 'ChatGPT',
}

interface AddAccountDialogProps {
  onClose: () => void
  /** Pre-select a provider when opening (e.g. for copilot reauth). Defaults to 'anthropic'. */
  initialProvider?: AddProvider
  /** Pre-fill the account name field (e.g. for copilot reauth of an existing account). */
  initialName?: string
}

export function AddAccountDialog({ onClose, initialProvider, initialName }: AddAccountDialogProps) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [name, setName] = useState(initialName ?? '')
  const [started, setStarted] = useState(false)
  // Optimistic local idle override — set when the user cancels.
  const [optimisticIdle, setOptimisticIdle] = useState(false)
  const [provider, setProvider] = useState<AddProvider>(initialProvider ?? 'anthropic')
  const [copilotStarted, setCopilotStarted] = useState(false)
  const [openaiStarted, setOpenaiStarted] = useState(false)

  const qc = useQueryClient()

  // Anthropic hooks
  const addAccount = useAddAccount()
  const cancelAdd = useCancelAddAccount()
  const submitAddCode = useSubmitAddAccountCode()
  const statusQuery = useAddAccountStatus(started && !optimisticIdle)
  const status = optimisticIdle ? undefined : statusQuery.data

  // Copilot hooks
  const addCopilotAccount = useAddCopilotAccount()
  const cancelCopilotAdd = useCancelCopilotAdd()
  const copilotStatusQuery = useCopilotAddStatus(name.trim(), copilotStarted)
  const copilotStatus = copilotStarted ? copilotStatusQuery.data : undefined
  const copilotStarting = addCopilotAccount.isPending

  // OpenAI hooks — the same trio, on the OpenAI routes.
  const addOpenAIAccount = useAddOpenAIAccount()
  const cancelOpenAIAdd = useCancelOpenAIAdd()
  const openaiStatusQuery = useOpenAIAddStatus(name.trim(), openaiStarted)
  const openaiStatus = openaiStarted ? openaiStatusQuery.data : undefined
  const openaiStarting = addOpenAIAccount.isPending

  // Anthropic add is busy from start until its code is exchanged (awaiting_code is the
  // paste step, not a finished state).
  const isAwaitingCode = status?.state === 'awaiting_code'
  const isBusy = status?.state === 'running' || status?.state === 'cancelling' || isAwaitingCode
  const isSuccess = status?.state === 'success'
  const isError = status?.state === 'error' || status?.state === 'timeout'

  // Refresh the accounts list once the Anthropic add transitions into success — the new
  // account only appears in the list after its slot is written.
  const prevStatusRef = useRef<string | undefined>(undefined)
  React.useEffect(() => {
    if (status?.state === 'success' && prevStatusRef.current !== 'success') {
      qc.invalidateQueries({ queryKey: QK.accounts })
    }
    prevStatusRef.current = status?.state
  }, [status?.state, qc])

  // Close when a device flow completes with authorization. One effect per flow
  // because each has its own `prev` cursor, but the same rule.
  const prevCopilotStatusRef = useRef<string | undefined>(undefined)
  React.useEffect(() => {
    if (copilotStatus?.state === 'authorized' && prevCopilotStatusRef.current !== 'authorized') {
      qc.invalidateQueries({ queryKey: QK.accounts })
      onClose()
    }
    prevCopilotStatusRef.current = copilotStatus?.state
  }, [copilotStatus?.state, qc, onClose])

  const prevOpenaiStatusRef = useRef<string | undefined>(undefined)
  React.useEffect(() => {
    if (openaiStatus?.state === 'authorized' && prevOpenaiStatusRef.current !== 'authorized') {
      qc.invalidateQueries({ queryKey: QK.accounts })
      onClose()
    }
    prevOpenaiStatusRef.current = openaiStatus?.state
  }, [openaiStatus?.state, qc, onClose])

  const trimmed = name.trim()
  // Mirror the backend rule client-side so an invalid name never round-trips to
  // a 400 that surfaces as a generic toast. null = valid (or empty).
  const nameError = accountNameError(trimmed)

  // Anthropic canSubmit: block while running
  const canSubmit = !!trimmed && !nameError && !isBusy
  // Device flows: block once the flow has started
  const copilotCanSubmit = !!trimmed && !nameError && !copilotStarted && !copilotStarting
  const openaiCanSubmit = !!trimmed && !nameError && !openaiStarted && !openaiStarting

  /** The device flow currently on screen, or null on the Anthropic tab. One
   *  accessor so the shared panel and the shared button row cannot disagree
   *  with each other about which provider is live. */
  const deviceFlow = provider === 'copilot'
    ? {
        started: copilotStarted,
        starting: copilotStarting,
        status: copilotStatus,
        canSubmit: copilotCanSubmit,
      }
    : provider === 'openai'
      ? {
          started: openaiStarted,
          starting: openaiStarting,
          status: openaiStatus,
          canSubmit: openaiCanSubmit,
        }
      : null
  const deviceFlowBusy = !!deviceFlow && (deviceFlow.started || deviceFlow.starting)

  function handleSubmit() {
    if (!canSubmit) return
    setOptimisticIdle(false)
    setStarted(true)
    addAccount.mutate(trimmed, {
      onError: () => setStarted(false),
    })
  }

  function handleCancel() {
    setOptimisticIdle(true)
    setStarted(false)
    cancelAdd.mutate()
  }

  function handleCopilotSubmit() {
    if (!copilotCanSubmit) return
    addCopilotAccount.mutate(trimmed, {
      onSuccess: () => setCopilotStarted(true),
      onError: () => setCopilotStarted(false),
    })
  }

  function handleCopilotCancel() {
    cancelCopilotAdd.mutate(trimmed)
    setCopilotStarted(false)
  }

  function handleOpenAISubmit() {
    if (!openaiCanSubmit) return
    addOpenAIAccount.mutate(trimmed, {
      onSuccess: () => setOpenaiStarted(true),
      onError: () => setOpenaiStarted(false),
    })
  }

  function handleOpenAICancel() {
    cancelOpenAIAdd.mutate(trimmed)
    setOpenaiStarted(false)
  }

  function submitCurrent() {
    if (provider === 'anthropic') handleSubmit()
    else if (provider === 'copilot') handleCopilotSubmit()
    else handleOpenAISubmit()
  }

  /** Switching provider cancels whatever the previous tab had running. Merely
   *  stopping its poll would leave a hidden browser/device-login process alive
   *  (and, for a new OpenAI account, a disabled placeholder in the manifest). */
  function pickProvider(next: AddProvider) {
    if (next === provider) return
    // A cancel cannot overtake a start request whose server-side flow does not
    // exist yet. Keep this tab fixed for the brief POST window, then the normal
    // cancel path becomes available once `started` is confirmed.
    if (copilotStarting || openaiStarting) return
    if (provider === 'anthropic' && started && !optimisticIdle) cancelAdd.mutate()
    if (provider === 'copilot' && copilotStarted) cancelCopilotAdd.mutate(trimmed)
    if (provider === 'openai' && openaiStarted) cancelOpenAIAdd.mutate(trimmed)
    setProvider(next)
    if (next !== 'anthropic') { setStarted(false); setOptimisticIdle(false) }
    if (next !== 'copilot') setCopilotStarted(false)
    if (next !== 'openai') setOpenaiStarted(false)
  }

  const providerTabs: { id: AddProvider; label: string }[] = [
    { id: 'anthropic', label: 'Anthropic (OAuth)' },
    { id: 'copilot', label: 'GitHub Copilot' },
    { id: 'openai', label: 'OpenAI (ChatGPT)' },
  ]

  return (
    <Dialog
      open
      onClose={onClose}
      title="Add account"
      size="sm"
      initialFocus={inputRef}
      dismissable={!isBusy && !copilotStarted && !openaiStarted
        && !copilotStarting && !openaiStarting}
    >
      {/* Provider toggle */}
      <div className="flex rounded-lg border border-edge overflow-hidden text-sm">
        {providerTabs.map((tab) => (
          <button
            key={tab.id}
            type="button"
            onClick={() => pickProvider(tab.id)}
            disabled={copilotStarting || openaiStarting}
            className={cn(
              'flex-1 py-2 px-3 text-center transition-colors min-h-target',
              provider === tab.id
                ? 'bg-accent text-accent-ink'
                : 'bg-surface text-ink hover:bg-surface-raised',
            )}
            aria-pressed={provider === tab.id}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <Field
        label="Account name"
        htmlFor="add-account-name"
        error={nameError || undefined}
        help={!nameError ? <>A short identifier (letters, numbers, <code>. _ -</code>). You can add a friendly nickname afterwards.</> : undefined}
        className="mt-4"
      >
        <Input
          ref={inputRef}
          id="add-account-name"
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              submitCurrent()
            }
          }}
          disabled={isBusy || isSuccess || copilotStarted || openaiStarted
            || copilotStarting || openaiStarting}
          placeholder="e.g. work"
          aria-label="Account name"
          invalid={!!nameError}
        />
      </Field>

      {/* Anthropic OAuth flow status */}
      {provider === 'anthropic' && isAwaitingCode && status?.authUrl && (
        <div className="mt-4">
          <CodePastePanel
            authUrl={status.authUrl}
            instructions="Open this link in your browser, where you're signed in to claude.ai, approve, then paste the code below."
            inputLabel={`Authorization code for ${trimmed}`}
            onSubmit={(code, onAccepted) => {
              submitAddCode.mutate(code, { onSuccess: onAccepted })
            }}
          />
        </div>
      )}
      {provider === 'anthropic' && isBusy && !isAwaitingCode && (
        <p className="mt-4 text-xs text-warning">
          Starting the claude.ai sign-in…
        </p>
      )}
      {provider === 'anthropic' && isSuccess && (
        <p className="mt-4 text-xs text-success">
          ✓ Account added.
        </p>
      )}
      {provider === 'anthropic' && isError && status?.message && (
        <p className="mt-4 text-xs text-danger" role="alert">
          {status.message}
        </p>
      )}

      {/* Device-flow status — one panel, both device providers. */}
      {deviceFlow && provider !== 'anthropic' && (
        <DeviceCodePanel
          state={deviceFlow.status}
          started={deviceFlowBusy}
          providerLabel={DEVICE_PROVIDER_LABEL[provider]}
        />
      )}

      <div className="flex gap-3 justify-end pt-4">
        {provider === 'anthropic' ? (
          <>
            {isBusy ? (
              <Button
                type="button"
                variant="secondary"
                onClick={handleCancel}
                aria-label="Cancel add"
              >
                Cancel
              </Button>
            ) : (
              <Button
                type="button"
                variant="secondary"
                onClick={onClose}
              >
                {isSuccess ? 'Done' : 'Cancel'}
              </Button>
            )}
            {!isSuccess && (
              <Button
                type="button"
                size="lg"
                onClick={handleSubmit}
                disabled={!canSubmit}
              >
                {isBusy ? 'Adding…' : 'Add'}
              </Button>
            )}
          </>
        ) : (
          <>
            {deviceFlowBusy ? (
              <Button
                type="button"
                variant="secondary"
                onClick={provider === 'copilot' ? handleCopilotCancel : handleOpenAICancel}
                aria-label="Cancel add"
                disabled={deviceFlow?.starting}
              >
                Cancel
              </Button>
            ) : (
              <Button
                type="button"
                variant="secondary"
                onClick={onClose}
              >
                Cancel
              </Button>
            )}
            <Button
              type="button"
              size="lg"
              onClick={submitCurrent}
              disabled={!deviceFlow?.canSubmit}
            >
              {deviceFlow?.starting
                ? 'Starting…'
                : deviceFlow?.started ? 'Authorizing…' : 'Add'}
            </Button>
          </>
        )}
      </div>
    </Dialog>
  )
}
