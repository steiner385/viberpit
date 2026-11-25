import type { UserFacingView } from '@/session/userFacingTools'
import { PushNotificationCard } from './PushNotificationCard'
import { ReportFindingsCard } from './ReportFindingsCard'
import { SendUserFileCard } from './SendUserFileCard'
import { SendUserMessageCard } from './SendUserMessageCard'

/** The card for one user-facing tool — the lazily loaded half of `UserFacingToolCard`. */
export function UserFacingToolBody({ view }: { view: UserFacingView }) {
  switch (view.name) {
    case 'SendUserFile': return <SendUserFileCard sent={view.sent} call={view.call} />
    case 'SendUserMessage': return <SendUserMessageCard message={view.message} call={view.call} />
    case 'ReportFindings': return <ReportFindingsCard findings={view.findings} call={view.call} />
    case 'PushNotification': return <PushNotificationCard message={view.message} />
  }
}
