/**
 * NewSessionDialogHost — the `?dialog=new` renderer.
 *
 * ## Why this file exists (Task 18, Item 0)
 *
 * The create-session form and the URL that opens it were split across two
 * components that Task 17 deleted: `App.tsx` read `?dialog=` and threaded it
 * down as a prop, and `Dashboard` owned the mutation, the 409 routing and the
 * navigate-on-success. `NewSessionDialog` survived the cutover; its host did
 * not, so `GlobalActions`' "New session" link wrote `?dialog=new` and NOTHING
 * rendered it — the rail's primary action was dead in every layout mode.
 *
 * The two halves live together here: the param is read through `dialogParam`
 * (the one owner of the `?dialog=` vocabulary) and everything the submit does is
 * in `handleSubmit` below. `NewSessionDialog` stays presentational — it takes
 * `onSubmit`/`isPending`/`externalError` and knows nothing about routing — so it
 * can still be mounted directly by a test that only cares about the form.
 *
 * ## Why it is mounted by `AppShell`, not by `SessionsPage`
 *
 * DEC-5: `?dialog=new` opens the dialog in every layout mode. The shell is the
 * one component mounted in every mode and on every route, which is also how it
 * behaved before the cutover — `App.tsx`'s `DashboardRoute` read `?dialog=` for
 * every tab path, not just `/sessions`. Mounting it on the Sessions page would
 * quietly narrow that to one route.
 *
 * ## Why the banner state is the shell's, not this component's
 *
 * A 409 "all accounts limited" turns into an app-level banner, and the shell
 * already owns that banner's state (it is also raised by `PromptDock`). This
 * component reports the reset time upward and holds no copy of it — a second
 * owner is how the banner came to have two auto-clear implementations in the
 * two old shells.
 *
 * ## Every other failure stays in the dialog
 *
 * The dialog is the one surface the user is looking at when the submit
 * fails, so the message renders there (`externalError`) and the dialog stays
 * open — including "no usable accounts", which used to close the dialog and
 * toast over the page header, and plain 5xx/network failures, which used to
 * reach the user only through the global mutation toast. The reading of the
 * error body is `describeCreateSessionFailure`, shared with `PromptDock`.
 */
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { useCreateSession, useSettings, type CreateSessionInput } from '@/api/hooks'
import type { Account } from '@/api/types'
import { sessionOpenState, sessionPath } from '@/session/sessionNavState'
import { useCloseDialog, useDialogName } from '@/shell/dialogParam'
import { NewSessionDialog } from './NewSessionDialog'
import { describeCreateSessionFailure } from './createSessionFailure'

export interface NewSessionDialogHostProps {
  accounts: Account[]
  /**
   * The 409 `earliestReset` when every account is at its limit — `null` when the
   * server could not say when capacity returns.
   */
  onAllLimited: (earliestReset: string | null) => void
}

export function NewSessionDialogHost({ accounts, onAllLimited }: NewSessionDialogHostProps) {
  const navigate = useNavigate()
  const closeDialog = useCloseDialog()
  const dialog = useDialogName()
  const createSession = useCreateSession()
  const { data: settingsData } = useSettings()
  // The failure the dialog shows inline. Set for every outcome except
  // "all accounts limited", which is the shell banner's to show.
  const [submitError, setSubmitError] = useState<string | null>(null)

  function close() {
    setSubmitError(null)
    closeDialog()
  }

  function handleSubmit(input: CreateSessionInput) {
    setSubmitError(null)
    createSession.mutate(input, {
      onSuccess: (session) => {
        closeDialog()
        if (session.usageUnverified) {
          toast("Couldn't verify account usage — started anyway.")
        }
        // Auto-open the new session and carry the prompt so it shows immediately,
        // before the SDK echoes it back.
        navigate(sessionPath(session.id), sessionOpenState(input.initialPrompt))
      },
      onError: (err) => {
        const failure = describeCreateSessionFailure(err)
        if (failure.kind === 'all-limited') {
          onAllLimited(failure.earliestReset)
          closeDialog()
          return
        }
        setSubmitError(failure.message)
      },
    })
  }

  if (dialog !== 'new') return null

  return (
    <NewSessionDialog
      accounts={accounts}
      onClose={close}
      onSubmit={handleSubmit}
      isPending={createSession.isPending}
      externalError={submitError}
      defaultPermissionMode={settingsData?.defaultPermissionMode}
    />
  )
}
