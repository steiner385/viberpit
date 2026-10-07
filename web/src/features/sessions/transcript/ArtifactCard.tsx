import { FileText, Image as ImageIcon, Link2 } from 'lucide-react'
import { artifactFromBlock, type Artifact, type ToolBlock } from '@/session/artifacts'
import { CopyablePath } from './CopyablePath'
import { ResultImage } from './ResultImages'

interface Props {
  block: ToolBlock
}

/**
 * The always-visible card for a `share_artifact` call: a session's deliverable
 * (PR URL, report path, screenshot) shown in full, never behind a collapsed tool
 * row. Built from the call input, so a URL or path shows the instant the call is
 * made; an image shows once the handler has ingested it and returned its served URL.
 */
export function ArtifactCard({ block }: Props) {
  const a = artifactFromBlock(block)
  return (
    <div className="my-2 rounded-lg border border-edge bg-surface-raised p-3 text-sm" data-testid="artifact-card">
      <div className="flex items-start gap-2">
        <span className="mt-0.5 shrink-0 text-ink-muted" aria-hidden="true"><KindIcon kind={a.kind} /></span>
        <div className="min-w-0 flex-1">
          <p className="font-medium text-ink">{a.label || a.value}</p>
          {a.note && <p className="mt-0.5 text-ink-muted">{a.note}</p>}
          <div className="mt-1.5"><Body a={a} /></div>
        </div>
      </div>
    </div>
  )
}

function KindIcon({ kind }: { kind: Artifact['kind'] }) {
  switch (kind) {
    case 'url': return <Link2 size={14} />
    case 'image': return <ImageIcon size={14} />
    default: return <FileText size={14} />
  }
}

// Only an http(s) value becomes an anchor. The server refuses anything else, but
// the card renders from the call INPUT before that verdict lands — a `javascript:`
// value must never be clickable even for the moment the result is pending.
const SAFE_HREF = /^https?:\/\//i

function Body({ a }: { a: Artifact }) {
  if (a.status === 'error') {
    return (
      <div>
        <p className="text-danger">{a.error}</p>
        {a.kind !== 'url' && a.value && <div className="mt-1"><CopyablePath text={a.value} /></div>}
      </div>
    )
  }
  switch (a.kind) {
    case 'url':
      return SAFE_HREF.test(a.value)
        ? <a href={a.value} target="_blank" rel="noopener noreferrer" className="break-all text-accent hover:underline">{a.value}</a>
        : <span className="break-all">{a.value}</span>
    case 'path':
      return <CopyablePath text={a.value} />
    case 'image':
      if (!a.url) {
        return (
          <div>
            <p className="text-ink-muted">Uploading image…</p>
            <div className="mt-1"><CopyablePath text={a.value} /></div>
          </div>
        )
      }
      return (
        <div>
          <ResultImage url={a.url} name={a.label} />
          <div className="mt-1 flex items-center gap-3 text-xs">
            <CopyablePath text={a.value} />
          </div>
        </div>
      )
  }
}
