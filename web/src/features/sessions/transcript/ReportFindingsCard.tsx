import { ListChecks } from 'lucide-react'
import type { ToolCallStatus } from '@/lib/toolContent'
import type { Finding, Findings, FindingSeverity } from '@/session/userFacingTools'
import { Badge, type Tone } from '@/ui/Badge'
import { PathRef } from './PathRef'
import { CallNotice, CardHeader, CARD_FRAME_CLASS } from './userFacingParts'

const SEVERITY: Record<FindingSeverity, { label: string; tone: Tone }> = {
  important: { label: 'Important', tone: 'danger' },
  pre_existing: { label: 'Pre-existing', tone: 'warning' },
  nit: { label: 'Nit', tone: 'neutral' },
}

/** A failure scenario longer than this folds behind a disclosure. */
const LONG_SCENARIO_CHARS = 240

/** `ReportFindings`: the review's findings in the order given — the contract says
 *  the session does not also print them as text, so this card is the only copy. */
export function ReportFindingsCard({ findings: { findings, level, malformed }, call }: { findings: Findings; call: ToolCallStatus }) {
  return (
    <div className={CARD_FRAME_CLASS} data-testid="report-findings-card">
      <CardHeader icon={<ListChecks size={14} />}>
        Findings <span className="font-normal text-ink-muted">· {findings.length}</span>
        {level && <Badge className="ml-2">{level}</Badge>}
      </CardHeader>
      {call.status === 'error' && <CallNotice call={call} pending="" />}
      {findings.length === 0
        ? <p className="mt-1 text-ink-muted">{malformed ? 'The findings could not be read.' : 'No findings reported.'}</p>
        : <ol className="mt-2 space-y-3">{findings.map((f, i) => <li key={i}><FindingRow f={f} /></li>)}</ol>}
    </div>
  )
}

function FindingRow({ f }: { f: Finding }) {
  const where = f.file ? (f.line ? `${f.file}:${f.line}` : f.file) : null
  return (
    <div className="border-l-2 border-edge pl-2">
      <div className="flex flex-wrap items-center gap-1.5">
        {f.severity && <Badge tone={SEVERITY[f.severity].tone}>{SEVERITY[f.severity].label}</Badge>}
        {where && <PathRef text={where} />}
        {f.verdict && <Badge tone="info">{f.verdict}</Badge>}
        {f.outcome && <Badge tone={f.outcome === 'fixed' ? 'success' : 'neutral'}>{f.outcome.replace(/_/g, ' ')}</Badge>}
      </div>
      {f.summary && <p className="mt-1 text-ink">{f.summary}</p>}
      {f.failureScenario && <Scenario text={f.failureScenario} />}
    </div>
  )
}

function Scenario({ text }: { text: string }) {
  if (text.length <= LONG_SCENARIO_CHARS) return <p className="mt-1 whitespace-pre-wrap text-ink-muted">{text}</p>
  return (
    <details className="mt-1 text-ink-muted">
      <summary className="cursor-pointer">Failure scenario</summary>
      <p className="mt-1 whitespace-pre-wrap">{text}</p>
    </details>
  )
}
