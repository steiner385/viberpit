/**
 * AccountsManager — self-contained accounts panel: account rows (status, usage,
 * reauth, rename, delete, refresh) plus the delete-confirmation dialog. Owns its
 * own data hooks so it can render inside the Dashboard tab panel (mobile) or the
 * desktop main pane unchanged.
 */

import * as React from 'react'
import { useState, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { RefreshCw, Pencil, Check, X, Trash2, Plus, KeyRound, Power } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Dialog } from '@/ui/Dialog'
import { Button } from '@/ui/Button'
import { UsageBadge, StatusPill } from './UsageBadge'
import {
  useAccounts,
  useSessions,
  useRefreshAccount,
  useUpdateAccount,
  useDeleteAccount,
  useSetAccountEnabled,
  useReauthAccount,
  useReauthStatus,
  useCancelReauth,
  useSubmitReauthCode,
  useOpenAIReauth,
  useOpenAIReauthStatus,
  useCancelOpenAIReauth,
  QK,
} from '@/api/hooks'
import type { AccountUpdate } from '@/api/hooks'
import { AddAccountDialog } from './AddAccountDialog'
import { DeviceCodePanel } from './DeviceCodePanel'
import type { Account, LiveSessionCounts, Session } from '@/api/types'
import { reserveWindow, usageUnavailableReason, usageWindows } from '@/lib/usageWindows'

// ── Sessions + Rank Badges ───────────────────────────────────────────────────

function SessionsBadge({ counts }: { counts?: LiveSessionCounts | null }) {
  const { active, idle } = counts ?? { active: 0, idle: 0 }
  let text: string
  if (active === 0 && idle === 0) text = '—'
  else if (active > 0 && idle > 0) text = `${active} active · ${idle} idle`
  else if (active > 0) text = `${active} active`
  else text = `${idle} idle`
  return (
    <span
      className={cn(
        'text-xs whitespace-nowrap',
        active === 0 && idle === 0 ? 'text-ink-muted' : 'text-ink-muted',
      )}
      aria-label="Live sessions using this account"
    >
      {text}
    </span>
  )
}

function RankBadge({ rank, reason }: { rank?: number | null; reason?: string | null }) {
  if (rank == null) {
    return <span className="text-xs text-ink-muted">unavailable</span>
  }
  const label = rank === 1 ? '#1 next' : `#${rank}`
  return (
    <span
      title={reason ?? undefined}
      className={cn(
        'text-xs font-medium rounded px-1.5 py-0.5 whitespace-nowrap',
        rank === 1
          ? 'bg-success-wash text-success'
          : 'text-ink-muted',
      )}
    >
      {label}
    </span>
  )
}

// ── Delete Account Dialog ────────────────────────────────────────────────────

interface DeleteAccountDialogProps {
  account: Account
  activeSessions: Session[]
  onClose: () => void
  onConfirm: () => void
  isPending: boolean
}

function DeleteAccountDialog({ account, activeSessions, onClose, onConfirm, isPending }: DeleteAccountDialogProps) {
  const cancelRef = useRef<HTMLButtonElement>(null)
  const displayLabel = account.nickname || account.name

  return (
    <Dialog open onClose={onClose} title="Delete account" size="sm" initialFocus={cancelRef}>
      <p className="text-sm text-ink-muted">
        Delete account <span className="font-medium text-ink">&lsquo;{displayLabel}&rsquo;</span>? This permanently
        removes its stored credentials and config directory. This can&rsquo;t be undone.
      </p>

      {activeSessions.length > 0 && (
        <p className="text-sm text-warning" role="alert">
          {activeSessions.length} active session{activeSessions.length === 1 ? '' : 's'} use this
          account and will lose access.
        </p>
      )}

      <div className="flex gap-3 justify-end pt-2">
        <Button ref={cancelRef} type="button" variant="secondary" onClick={onClose} disabled={isPending}>
          Cancel
        </Button>
        <Button type="button" variant="danger" size="lg" onClick={onConfirm} loading={isPending}>
          {isPending ? 'Deleting…' : 'Delete'}
        </Button>
      </div>
    </Dialog>
  )
}

// ── Provider Chip ────────────────────────────────────────────────────────────

function CopilotChip() {
  return (
    <span
      className={cn(
        'text-xs font-medium rounded px-1.5 py-0.5 whitespace-nowrap',
        'bg-info-wash text-info',
      )}
      aria-label="GitHub Copilot account"
    >
      Copilot
    </span>
  )
}

function OpenAIChip() {
  return (
    <span
      className={cn(
        'text-xs font-medium rounded px-1.5 py-0.5 whitespace-nowrap',
        'bg-success-wash text-success',
      )}
      aria-label="OpenAI (ChatGPT) account"
    >
      OpenAI
    </span>
  )
}

/**
 * Codex-only health: is the credential authority reachable, how many runners
 * hold this account's credential, and why it is degraded.
 *
 * Quota is NOT rendered here — an OpenAI account's windows already arrive in
 * `account.usage` and are drawn by the generic `usageWindows` badge row above,
 * which is the whole point of the provider-neutral window list. Re-deriving a
 * "premium requests" number here would be the parallel implementation that
 * shape exists to prevent.
 *
 * `authority: null` is tri-state like host health: not configured, or the probe
 * itself failed. Saying "unreachable" for it would accuse a service the edge
 * never asked.
 */
function CodexHealthLine({ account }: { account: Account }) {
  const codex = account.codex
  if (!codex) return null
  const runners = codex.activeRunners ?? 0
  const authority = codex.authority
  const authorityText = authority == null
    ? 'Authority status unknown'
    : authority.reachable ? 'Authority reachable' : 'Authority unreachable'
  return (
    <div className="flex items-center flex-wrap gap-x-2 gap-y-1 text-xs">
      <span
        className={cn(
          'whitespace-nowrap',
          authority == null
            ? 'text-ink-muted'
            : authority.reachable
              ? 'text-success'
              : 'text-danger',
        )}
        title={authority?.detail ?? undefined}
      >
        {authorityText}
      </span>
      <span className="text-ink-muted whitespace-nowrap">
        {runners === 1 ? '1 active runner' : `${runners} active runners`}
      </span>
      {codex.degradedReason && (
        <span
          className={cn(
            'rounded px-1.5 py-0.5 font-medium whitespace-nowrap',
            'bg-warning-wash text-warning',
          )}
          role="status"
        >
          {codex.degradedReason}
        </span>
      )}
    </div>
  )
}

function OverReserveChip() {
  return (
    <span
      className={cn(
        'text-xs font-medium rounded px-1.5 py-0.5 whitespace-nowrap',
        'bg-warning-wash text-warning',
      )}
      title="Weekly usage is at/over this account's reserve — only picked as a last resort"
    >
      over reserve
    </span>
  )
}

function ExtraUsageChip() {
  return (
    <span
      className={cn(
        'text-xs font-medium rounded px-1.5 py-0.5 whitespace-nowrap',
        'bg-warning-wash text-warning',
      )}
      title="At its ceiling, this account keeps being selected and spends paid extra-usage credits — but only once every other account is exhausted"
    >
      extra usage
    </span>
  )
}

function ExtraUsageUnavailableChip() {
  return (
    <span
      className={cn(
        'text-xs font-medium rounded px-1.5 py-0.5 whitespace-nowrap',
        'bg-danger-wash text-danger',
      )}
      title="Extra usage is switched on here but off in the Anthropic console, so no credits can be spent. Enable it there, or turn this off."
    >
      not enabled upstream
    </span>
  )
}

// ── Account Row ──────────────────────────────────────────────────────────────

interface AccountRowProps {
  account: Account
  onRefresh: (name: string) => void
  refreshing: boolean
  onSave: (update: AccountUpdate) => void
  onDelete: (account: Account) => void
  /** Called when a Copilot account's reauth button is clicked — opens the device-flow dialog. */
  onCopilotReauth?: (account: Account) => void
}

function AccountRow({ account, onRefresh, refreshing, onSave, onDelete, onCopilotReauth }: AccountRowProps) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [reserveDraft, setReserveDraft] = useState('')
  const [extraUsageDraft, setExtraUsageDraft] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  // Reauth state: track whether we've started a reauth (controls polling)
  const [reauthStarted, setReauthStarted] = useState(false)
  // Optimistic local idle override — set when user cancels
  const [reauthOptimisticIdle, setReauthOptimisticIdle] = useState(false)
  // Code input for awaiting_code state
  const [code, setCode] = useState('')

  const [modelApprovalDraft, setModelApprovalDraft] = useState<string>(
    account.accountDefaults?.modelApprovalAction ?? '',
  )

  const qc = useQueryClient()
  const reauthAccount = useReauthAccount()
  const cancelReauth = useCancelReauth()
  const submitCode = useSubmitReauthCode()
  const setEnabled = useSetAccountEnabled()
  const updateAccount = useUpdateAccount()

  // Absent `enabled` (older payloads) means enabled — only an explicit false disables.
  const isDisabled = account.enabled === false

  // ── OpenAI re-auth is a DEVICE FLOW, not the browser OAuth ────────────────
  //
  // An OpenAI account has no local credential slot to re-encrypt: it
  // re-authenticates by re-running its device login at the credential
  // authority. Same `/accounts/{name}/reauth` URLs, different payload — hence
  // its own typed hooks and its own poll key.
  const isOpenAI = account.provider === 'openai'
  const [deviceReauthStarted, setDeviceReauthStarted] = useState(false)
  const openaiReauth = useOpenAIReauth()
  const cancelOpenAIReauth = useCancelOpenAIReauth()
  const deviceReauthStarting = openaiReauth.isPending
  const deviceStatusQuery = useOpenAIReauthStatus(account.name, isOpenAI && deviceReauthStarted)
  const deviceStatus = deviceReauthStarted ? deviceStatusQuery.data : undefined

  // Poll status while reauth is running (enabled when started and not optimistically idled)
  const reauthStatusQuery = useReauthStatus(
    account.name, !isOpenAI && reauthStarted && !reauthOptimisticIdle)
  const reauthStatus = reauthOptimisticIdle ? undefined : reauthStatusQuery.data

  const needsReauth = account.status === 'needs-reauth' || account.status === 'needs-relogin'

  // A finished device login is a new credential: refresh the row, and stop
  // polling by clearing the flag (the query stops on its own at terminal states).
  const prevDeviceStateRef = useRef<string | undefined>(undefined)
  React.useEffect(() => {
    const state = deviceStatus?.state
    if (state === 'authorized' && prevDeviceStateRef.current !== 'authorized') {
      qc.invalidateQueries({ queryKey: QK.accounts })
      setDeviceReauthStarted(false)
    }
    prevDeviceStateRef.current = state
  }, [deviceStatus?.state, qc])

  function handleOpenAIReauth() {
    openaiReauth.mutate(account.name, {
      onSuccess: () => setDeviceReauthStarted(true),
      onError: () => setDeviceReauthStarted(false),
    })
  }

  function handleOpenAICancel() {
    setDeviceReauthStarted(false)
    cancelOpenAIReauth.mutate(account.name)
  }

  function handleReauth() {
    setReauthOptimisticIdle(false)
    setReauthStarted(true)
    reauthAccount.mutate(account.name, {
      onError: () => {
        // If start itself fails, stop polling
        setReauthStarted(false)
      },
    })
  }

  function handleCancel() {
    // Optimistically return to idle immediately, don't wait for poll
    setReauthOptimisticIdle(true)
    setReauthStarted(false)
    cancelReauth.mutate(account.name, {
      onError: () => {
        // Even on cancel error, keep optimistic idle
      },
    })
  }

  // Refresh the accounts list when reauth transitions into success. Polling stops
  // on its own once the status is terminal (see useReauthStatus.refetchInterval),
  // so this effect only reacts to the external query state — no setState here.
  const prevStatusRef = useRef<string | undefined>(undefined)
  React.useEffect(() => {
    const state = reauthStatus?.state
    if (state === 'success' && prevStatusRef.current !== 'success') {
      qc.invalidateQueries({ queryKey: QK.accounts })
    }
    prevStatusRef.current = state
  }, [reauthStatus?.state, qc])

  function startEdit() {
    setDraft(account.nickname ?? '')
    setReserveDraft(account.weeklyReservePct != null ? String(account.weeklyReservePct) : '')
    setExtraUsageDraft(account.allowExtraUsage === true)
    setEditing(true)
    setTimeout(() => inputRef.current?.focus(), 0)
  }

  function cancelEdit() {
    setEditing(false)
    setDraft('')
    setReserveDraft('')
    setExtraUsageDraft(false)
  }

  function saveEdit() {
    const trimmed = reserveDraft.trim()
    let reserve: number | null = null
    if (trimmed !== '') {
      const parsed = Number(trimmed)
      if (Number.isFinite(parsed) && parsed > 0) reserve = Math.min(100, Math.max(1, parsed))
    }
    onSave({
      name: account.name,
      nickname: draft.trim() || null,
      weeklyReservePct: reserve,
      allowExtraUsage: extraUsageDraft,
    })
    setEditing(false)
    setDraft('')
    setReserveDraft('')
    setExtraUsageDraft(false)
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Enter') { e.preventDefault(); saveEdit() }
    if (e.key === 'Escape') { e.preventDefault(); cancelEdit() }
  }

  const displayLabel = account.nickname || account.name

  const isReauthRunning = reauthStatus?.state === 'running' || reauthStatus?.state === 'cancelling'
  const isAwaitingCode = reauthStatus?.state === 'awaiting_code'
  const isReauthSuccess = reauthStatus?.state === 'success'
  const isReauthError = reauthStatus?.state === 'error' || reauthStatus?.state === 'timeout'

  return (
    <div
      className={cn(
        'flex flex-col gap-1 rounded-lg border border-edge',
        'bg-surface px-3 py-2',
      )}
    >
      {/* Top row: account name / edit inputs + action icon buttons */}
      <div className="flex items-center gap-2">
        {editing ? (
          <div className="flex-1 flex items-center gap-2 min-w-0">
            <input
              ref={inputRef}
              type="text"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={handleKeyDown}
              aria-label={`Nickname for ${account.name}`}
              placeholder={account.name}
              className={cn(
                'flex-1 min-w-0 rounded border border-edge-strong',
                'bg-surface text-ink',
                'px-2 py-1 text-sm',
                'focus:outline-none focus:ring-2 focus:ring-accent',
              )}
            />
            <input
              type="number"
              min={1}
              max={100}
              value={reserveDraft}
              onChange={(e) => setReserveDraft(e.target.value)}
              onKeyDown={handleKeyDown}
              aria-label={`Weekly reserve % for ${account.name}`}
              placeholder="reserve %"
              title="Stop auto-selecting this account once weekly usage reaches this % (blank = no reserve)"
              className={cn(
                'w-24 rounded border border-edge-strong',
                'bg-surface text-ink',
                'px-2 py-1 text-sm',
                'focus:outline-none focus:ring-2 focus:ring-accent',
              )}
            />
            {/* Behind the edit form rather than a one-click row button (unlike the
                enable/disable Power toggle): opting in spends real money, so it
                gets a deliberate save step. */}
            <label
              className={cn(
                'flex items-center gap-1.5 text-xs whitespace-nowrap',
                'text-ink-muted',
              )}
              title="When every other account is at its usage ceiling, keep selecting this one and spend paid extra-usage credits"
            >
              <input
                type="checkbox"
                checked={extraUsageDraft}
                onChange={(e) => setExtraUsageDraft(e.target.checked)}
                onKeyDown={handleKeyDown}
                aria-label={`Allow extra usage credits for ${account.name}`}
                className="h-4 w-4 rounded border-edge-strong accent-warning"
              />
              extra credits
            </label>
            <button
              type="button"
              onClick={saveEdit}
              aria-label="Save account settings"
              className={cn(
                'min-h-target min-w-target flex items-center justify-center rounded',
                'text-success',
                'hover:bg-surface-raised',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
              )}
            >
              <Check size={14} aria-hidden="true" />
            </button>
            <button
              type="button"
              onClick={cancelEdit}
              aria-label="Cancel rename"
              className={cn(
                'min-h-target min-w-target flex items-center justify-center rounded',
                'text-ink-muted',
                'hover:bg-surface-raised',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
              )}
            >
              <X size={14} aria-hidden="true" />
            </button>
          </div>
        ) : (
          <>
            <div className="flex-1 min-w-0 flex items-center gap-2">
              <span className={cn(
                'text-sm font-medium text-ink truncate',
                isDisabled && 'opacity-50 line-through',
              )}>
                {displayLabel}
              </span>
              {account.provider === 'copilot' && <CopilotChip />}
              {isOpenAI && <OpenAIChip />}
            </div>
            {/* Action icon buttons — always icon-only so they don't crowd the name */}
            <div className="flex items-center gap-1 flex-shrink-0">
              <button
                type="button"
                className={cn(
                  'min-h-target min-w-target flex items-center justify-center rounded',
                  isDisabled ? 'text-success' : 'text-ink-muted',
                  'hover:bg-surface-raised',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
                  'disabled:opacity-40',
                )}
                aria-label={`${isDisabled ? 'Enable' : 'Disable'} account ${account.name}`}
                title={isDisabled
                  ? 'Re-enable this account for selection'
                  : 'Pause this account — kept intact but never selected until re-enabled'}
                disabled={setEnabled.isPending}
                onClick={() => setEnabled.mutate({ name: account.name, enabled: isDisabled })}
              >
                <Power size={14} aria-hidden="true" />
              </button>
              <button
                type="button"
                className={cn(
                  'min-h-target min-w-target flex items-center justify-center rounded',
                  'text-ink-muted',
                  'hover:bg-surface-raised',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
                )}
                aria-label={`Rename ${account.name}`}
                onClick={startEdit}
              >
                <Pencil size={14} aria-hidden="true" />
              </button>
              <button
                type="button"
                className={cn(
                  'min-h-target min-w-target flex items-center justify-center rounded',
                  'text-danger',
                  'hover:bg-danger-wash',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-danger',
                )}
                aria-label={`Delete account ${account.name}`}
                onClick={() => onDelete(account)}
              >
                <Trash2 size={14} aria-hidden="true" />
              </button>
              <button
                type="button"
                className={cn(
                  'h-8 w-8 flex items-center justify-center rounded',
                  'text-ink-muted',
                  'hover:bg-surface-raised',
                  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
                  'disabled:opacity-40',
                )}
                aria-label={`Refresh account ${account.name}`}
                disabled={refreshing}
                onClick={() => onRefresh(account.name)}
              >
                <RefreshCw
                  size={14}
                  aria-hidden="true"
                  className={cn(refreshing && 'animate-spin')}
                />
              </button>
            </div>
          </>
        )}
      </div>

      {/* Badge row: status + usage + reauth — wraps naturally on narrow screens */}
      {!editing && (
        <div className="flex items-center flex-wrap gap-x-2 gap-y-1">
          <StatusPill status={account.status} />
          {isDisabled && (
            <span
              className="rounded px-1.5 py-0.5 text-xs font-medium bg-surface-raised text-ink-muted"
            >
              Disabled
            </span>
          )}
          <SessionsBadge counts={account.liveSessions} />
          <RankBadge rank={account.rank} reason={account.rankReason} />
          {account.weeklyReservePct != null &&
            (reserveWindow(account.usage)?.pct ?? -1) >= account.weeklyReservePct &&
            <OverReserveChip />}
          {account.allowExtraUsage === true && <ExtraUsageChip />}
          {/* Opted in, but the provider has extra usage off — the toggle cannot
              take effect, so say so rather than let it look armed. Usage is only
              checked when known: a failed fetch is not evidence it is disabled. */}
          {account.allowExtraUsage === true && account.usage != null &&
            account.usage.extraUsageEnabled === false && <ExtraUsageUnavailableChip />}
          {/* One badge per window the provider actually reported. A Copilot
              account has ONE (premium requests); it used to render that same
              number twice, as "premium-req" and again as "weekly". The reserve
              marker rides on whichever window the reserve is measured against. */}
          {usageWindows(account.usage).map((w) => (
            <UsageBadge
              key={w.key}
              pct={w.pct}
              label={w.label}
              resetsAt={w.resetsAt}
              staleAsOf={account.usage?.staleAsOf}
              markerPct={
                w.key === reserveWindow(account.usage)?.key
                  ? account.weeklyReservePct ?? null
                  : null
              }
            />
          ))}
          {/* With no windows the row was simply blank, which read as "still
              loading" for an account whose quota is permanently unreadable.
              Name the reason instead — the account is fine, its credential just
              cannot answer the usage endpoint. */}
          {usageUnavailableReason(account.usage) && (
            <span className="text-xs text-ink-muted" data-testid="usage-unavailable">
              {usageUnavailableReason(account.usage)}
            </span>
          )}
          {/* Both device-flow providers get the same control and the same label
              — it IS the same action. Copilot re-runs it through the add dialog
              (its add route tolerates an existing slot); OpenAI runs it in place
              on the per-account reauth routes, since `/accounts/add/openai`
              409s on a name that already exists. */}
          {account.provider === 'copilot' ? (
            !isReauthRunning && !isAwaitingCode && (
              <button
                type="button"
                className={cn(
                  'rounded min-h-target focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-warning',
                  needsReauth
                    ? 'px-2 py-1 text-xs font-medium bg-warning-strong text-on-strong hover:bg-warning-strong'
                    : 'min-w-target flex items-center justify-center text-ink-muted hover:text-warning hover:bg-surface-raised',
                )}
                aria-label={`Re-run device flow for ${account.name}`}
                onClick={() => onCopilotReauth?.(account)}
              >
                {needsReauth ? 'Re-authenticate' : <KeyRound size={14} aria-hidden="true" />}
              </button>
            )
          ) : isOpenAI ? (
            !deviceReauthStarted && !deviceReauthStarting && (
              <button
                type="button"
                className={cn(
                  'rounded min-h-target focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-warning',
                  needsReauth
                    ? 'px-2 py-1 text-xs font-medium bg-warning-strong text-on-strong hover:bg-warning-strong'
                    : 'min-w-target flex items-center justify-center text-ink-muted hover:text-warning hover:bg-surface-raised',
                )}
                aria-label={`Re-run device flow for ${account.name}`}
                onClick={handleOpenAIReauth}
              >
                {needsReauth ? 'Re-authenticate' : <KeyRound size={14} aria-hidden="true" />}
              </button>
            )
          ) : (
            !isReauthRunning && !isAwaitingCode && (
              <button
                type="button"
                className={cn(
                  'rounded min-h-target focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-warning',
                  needsReauth
                    ? 'px-2 py-1 text-xs font-medium bg-warning-strong text-on-strong hover:bg-warning-strong'
                    : 'min-w-target flex items-center justify-center text-ink-muted hover:text-warning hover:bg-surface-raised',
                )}
                aria-label={`Re-authenticate ${account.name}`}
                onClick={handleReauth}
              >
                {needsReauth ? 'Re-authenticate' : <KeyRound size={14} aria-hidden="true" />}
              </button>
            )
          )}
          {(isReauthRunning || isAwaitingCode || deviceReauthStarted
            || deviceReauthStarting) && (
            <button
              type="button"
              className={cn(
                'rounded px-2 py-1 text-xs font-medium min-h-target',
                'border border-edge-strong',
                'text-ink',
                'hover:bg-surface-raised',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
              )}
              aria-label="Cancel reauth"
              onClick={deviceReauthStarted ? handleOpenAICancel : handleCancel}
              disabled={deviceReauthStarting}
            >
              Cancel
            </button>
          )}
        </div>
      )}

      {/* Codex authority health + live runner count — OpenAI accounts only. */}
      {!editing && isOpenAI && <CodexHealthLine account={account} />}

      {/* The device code itself, in the SAME panel the add dialog uses. */}
      {!editing && isOpenAI && (
        <DeviceCodePanel
          state={deviceStatus}
          started={deviceReauthStarted || deviceReauthStarting}
          providerLabel="ChatGPT"
        />
      )}

      {/* Reauth status line */}
      {!editing && isReauthRunning && (
        <p className="text-xs text-warning">
          {reauthStatus?.message ?? 'Verifying the pasted code…'}
        </p>
      )}
      {!editing && isAwaitingCode && reauthStatus?.authUrl && (
        <div className="flex flex-col gap-2 mt-1">
          <p className="text-xs text-warning">
            Open this URL, approve, then paste the code below.
          </p>
          <div className="flex items-center gap-2 min-w-0">
            <a
              href={reauthStatus.authUrl}
              target="_blank"
              rel="noopener noreferrer"
              className={cn(
                'text-xs text-accent underline truncate min-w-0 flex-1',
              )}
            >
              {reauthStatus.authUrl}
            </a>
            <button
              type="button"
              className={cn(
                'rounded px-2 py-1 text-xs flex-shrink-0',
                'border border-edge-strong',
                'text-ink',
                'hover:bg-surface-raised',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
              )}
              onClick={() => { void navigator.clipboard.writeText(reauthStatus.authUrl!) }}
            >
              Copy
            </button>
          </div>
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              aria-label={`Authorization code for ${account.name}`}
              placeholder="Paste authorization code…"
              className={cn(
                'flex-1 min-w-0 rounded border border-edge-strong',
                'bg-surface text-ink',
                'px-2 py-1 text-xs',
                'focus:outline-none focus:ring-2 focus:ring-accent',
              )}
            />
            <button
              type="button"
              className={cn(
                'rounded px-2 py-1 text-xs font-medium flex-shrink-0',
                'bg-accent text-accent-ink hover:bg-accent/90',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
                'disabled:opacity-40',
              )}
              disabled={!code.trim()}
              onClick={() => {
                submitCode.mutate(
                  { name: account.name, code: code.trim() },
                  { onSuccess: () => setCode('') },
                )
              }}
            >
              Submit code
            </button>
          </div>
        </div>
      )}
      {!editing && isReauthSuccess && (
        <p className="text-xs text-success">
          ✓ Re-authentication successful — account refreshed.
        </p>
      )}
      {/* A success can still carry a caveat: the minted token launches sessions
          but cannot read usage (no user:profile scope). Say so here, at the
          moment it is actionable, rather than only as a blank quota badge. */}
      {!editing && isReauthSuccess && reauthStatus?.message && (
        <p className="text-xs text-warning" role="status">
          {reauthStatus.message}
        </p>
      )}
      {!editing && isReauthError && reauthStatus?.message && (
        <p className="text-xs text-danger" role="alert">
          {reauthStatus.message}
        </p>
      )}

      {/* Per-account model approval override — always visible, saves immediately on change */}
      {!editing && (
        <div className="flex items-center gap-2 pt-1">
          <label
            htmlFor={`account-model-approval-${account.name}`}
            className="text-xs text-ink-muted whitespace-nowrap"
          >
            Model approval:
          </label>
          <select
            id={`account-model-approval-${account.name}`}
            aria-label={`Model approval action for ${account.name}`}
            value={modelApprovalDraft}
            onChange={(e) => {
              const val = e.target.value
              setModelApprovalDraft(val)
              updateAccount.mutate({
                name: account.name,
                modelApprovalAction: val || null,
              })
            }}
            className={cn(
              'flex-1 min-w-0 rounded border border-edge-strong',
              'bg-surface text-ink',
              'px-2 py-1 text-xs',
              'focus:outline-none focus:ring-2 focus:ring-accent',
            )}
          >
            <option value="">Inherit (global default)</option>
            <option value="require_approval">Require approval</option>
            <option value="block">Block</option>
            <option value="allow">Allow</option>
          </select>
        </div>
      )}
    </div>
  )
}

// ── Accounts Strip ───────────────────────────────────────────────────────────

interface AccountsStripProps {
  accounts: Account[]
  onRefresh: (name: string) => void
  refreshingName: string | null
  onSave: (update: AccountUpdate) => void
  onDelete: (account: Account) => void
  onCopilotReauth: (account: Account) => void
}

function AccountsStrip({ accounts, onRefresh, refreshingName, onSave, onDelete, onCopilotReauth }: AccountsStripProps) {
  if (accounts.length === 0) return null

  return (
    <div className="flex flex-col gap-2">
      {accounts.map((account) => (
        <AccountRow
          key={account.name}
          account={account}
          onRefresh={onRefresh}
          refreshing={refreshingName === account.name}
          onSave={onSave}
          onDelete={onDelete}
          onCopilotReauth={onCopilotReauth}
        />
      ))}
    </div>
  )
}

// ── AccountsManager ──────────────────────────────────────────────────────────

export function AccountsManager() {
  const { data: accounts = [], isLoading: accountsLoading } = useAccounts()
  const { data: sessions = [] } = useSessions()
  const refreshAccount = useRefreshAccount()
  const updateAccount = useUpdateAccount()
  const deleteAccount = useDeleteAccount()

  const [refreshingAccount, setRefreshingAccount] = useState<string | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<Account | null>(null)
  const [showAdd, setShowAdd] = useState(false)
  /** Non-null when a Copilot account's reauth button was clicked — drives AddAccountDialog pre-selection. */
  const [copilotReauthAccount, setCopilotReauthAccount] = useState<Account | null>(null)

  function handleRefreshAccount(name: string) {
    setRefreshingAccount(name)
    refreshAccount.mutate(name, {
      onSettled: () => setRefreshingAccount(null),
    })
  }

  function handleSave(update: AccountUpdate) {
    updateAccount.mutate(update)
  }

  function handleDeleteAccount(account: Account) {
    setDeleteTarget(account)
  }

  function confirmDelete() {
    if (!deleteTarget) return
    deleteAccount.mutate(deleteTarget.name, {
      onSuccess: () => setDeleteTarget(null),
      onError: () => setDeleteTarget(null),
    })
  }

  const deleteTargetSessions = deleteTarget
    ? sessions.filter((s) => s.accountName === deleteTarget.name)
    : []

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-ink-muted">
          Accounts
        </h2>
        <Button
          type="button"
          variant="secondary"
          size="sm"
          onClick={() => setShowAdd(true)}
          className="gap-1.5"
        >
          <Plus size={14} aria-hidden="true" />
          Add account
        </Button>
      </div>
      {!accountsLoading && (
        <AccountsStrip
          accounts={accounts}
          onRefresh={handleRefreshAccount}
          refreshingName={refreshingAccount}
          onSave={handleSave}
          onDelete={handleDeleteAccount}
          onCopilotReauth={(acct) => setCopilotReauthAccount(acct)}
        />
      )}

      {showAdd && <AddAccountDialog onClose={() => setShowAdd(false)} />}
      {copilotReauthAccount && (
        <AddAccountDialog
          onClose={() => setCopilotReauthAccount(null)}
          initialProvider="copilot"
          initialName={copilotReauthAccount.name}
        />
      )}

      {deleteTarget && (
        <DeleteAccountDialog
          account={deleteTarget}
          activeSessions={deleteTargetSessions}
          onClose={() => setDeleteTarget(null)}
          onConfirm={confirmDelete}
          isPending={deleteAccount.isPending}
        />
      )}
    </div>
  )
}
