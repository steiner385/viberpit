/**
 * AddAccountDialog — collects a name for a new account, then drives either:
 *   - Anthropic OAuth: the interactive browser-OAuth login (the same global
 *     state machine as reauth) to create the account's credential slot.
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
import { ReauthBrowserPanel } from './ReauthBrowserPanel'
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
  const statusQuery = useAddAccountStatus(started && !optimisticIdle)
  const status = optimisticIdle ? undefined : statusQuery.data

  // Copilot hooks
  const addCopilotAccount = useAddCopilotAccount()
  const cancelCopilotAdd = useCancelCopilotAdd()
  const copilotStatusQuery = useCopilotAddStatus(name.trim(), copilotStarted)
  const copilotStatus = copilotStarted ? copilotStatusQuery.data : undefined

  // OpenAI hooks — the same trio, on the OpenAI routes.
  const addOpenAIAccount = useAddOpenAIAccount()
  const cancelOpenAIAdd = useCancelOpenAIAdd()
  const openaiStatusQuery = useOpenAIAddStatus(name.trim(), openaiStarted)
  const openaiStatus = openaiStarted ? openaiStatusQuery.data : undefined

  const isRunning = status?.state === 'running' || status?.state === 'cancelling'
  const isSuccess = status?.state === 'success'
  const isError = status?.state === 'error' || status?.state === 'timeout'
  // While the in-page reauth browser is showing, widen the dialog so the 1280×800
  // virtual display isn't crushed into the narrow add-account form width.
  const showVnc = isRunning && !!status?.vncSession

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
  const canSubmit = !!trimmed && !nameError && !isRunning
  // Device flows: block once the flow has started
  const copilotCanSubmit = !!trimmed && !nameError && !copilotStarted
  const openaiCanSubmit = !!trimmed && !nameError && !openaiStarted

  /** The device flow currently on screen, or null on the Anthropic tab. One
   *  accessor so the shared panel and the shared button row cannot disagree
   *  with each other about which provider is live. */
  const deviceFlow = provider === 'copilot'
    ? { started: copilotStarted, status: copilotStatus, canSubmit: copilotCanSubmit }
    : provider === 'openai'
      ? { started: openaiStarted, status: openaiStatus, canSubmit: openaiCanSubmit }
      : null

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
    setCopilotStarted(true)
    addCopilotAccount.mutate(trimmed, {
      onError: () => setCopilotStarted(false),
    })
  }

  function handleCopilotCancel() {
    cancelCopilotAdd.mutate(trimmed)
    setCopilotStarted(false)
  }

  function handleOpenAISubmit() {
    if (!openaiCanSubmit) return
    setOpenaiStarted(true)
    addOpenAIAccount.mutate(trimmed, {
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

  /** Switching provider abandons whatever the previous tab had running — the
   *  cancel routes are per-provider, so leaving a flag set would keep polling a
   *  flow the user can no longer see. */
  function pickProvider(next: AddProvider) {
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
      size={showVnc ? 'xl' : 'sm'}
      initialFocus={inputRef}
      dismissable={!isRunning && !copilotStarted && !openaiStarted}
      className="transition-[max-width] motion-reduce:transition-none"
    >
      {/* Provider toggle */}
      <div className="flex rounded-lg border border-edge overflow-hidden text-sm">
        {providerTabs.map((tab) => (
          <button
            key={tab.id}
            type="button"
            onClick={() => pickProvider(tab.id)}
            className={cn(
              'flex-1 py-2 px-3 text-center transition-colors min-h-[44px]',
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
          disabled={isRunning || isSuccess || copilotStarted || openaiStarted}
          placeholder="e.g. work"
          aria-label="Account name"
          invalid={!!nameError}
        />
      </Field>

      {/* Anthropic OAuth flow status */}
      {provider === 'anthropic' && isRunning && (
        status?.vncSession ? (
          <>
            <p className="mt-4 text-xs text-amber-600 dark:text-amber-400">
              Sign in to Claude in the window below…
            </p>
            <ReauthBrowserPanel vncSession={status.vncSession} />
          </>
        ) : (
          <p className="mt-4 text-xs text-amber-600 dark:text-amber-400">
            Opening Chrome on the host screen — sign in there…
          </p>
        )
      )}
      {provider === 'anthropic' && isSuccess && (
        <p className="mt-4 text-xs text-green-600 dark:text-green-400">
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
          started={deviceFlow.started}
          providerLabel={DEVICE_PROVIDER_LABEL[provider]}
        />
      )}

      <div className="flex gap-3 justify-end pt-4">
        {provider === 'anthropic' ? (
          <>
            {isRunning ? (
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
                {isRunning ? 'Adding…' : 'Add'}
              </Button>
            )}
          </>
        ) : (
          <>
            {deviceFlow?.started ? (
              <Button
                type="button"
                variant="secondary"
                onClick={provider === 'copilot' ? handleCopilotCancel : handleOpenAICancel}
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
                Cancel
              </Button>
            )}
            <Button
              type="button"
              size="lg"
              onClick={submitCurrent}
              disabled={!deviceFlow?.canSubmit}
            >
              {deviceFlow?.started ? 'Authorizing…' : 'Add'}
            </Button>
          </>
        )}
      </div>
    </Dialog>
  )
}
