import { useSessionFile } from '@/api/hooks'
import { CopyablePath } from './CopyablePath'
import { worktreeRelativePath } from './filePathRef'
import { useFileRefActions, type FileRefActions } from './fileRefContext'
import { IMAGE_NOTICE_CLASS, RESULT_IMAGE_CLASS } from './ResultImages'

interface Props {
  /** The image's path on the session host, as written (absolute, or cwd-relative). */
  path: string
  alt?: string
  /** Inside a link: the bare image, no control of its own — one click target. */
  linked?: boolean
}

/** The raster types the file read sniffs — the same set `ResultImage` loads from a `data:` URL. */
const RASTER = /^image\/(?:png|jpeg|gif|webp)$/

/**
 * An image that is a FILE on the session host — a markdown `![](shot.png)`, not
 * a URL the browser can fetch. Under the live session's worktree it is read
 * through the Files API and shown inline, a click opening it in Files; anywhere
 * else (outside the worktree, or no provider: archived reader, sub-agent page,
 * assistant drawer) it is a notice naming the path — only `share_artifact`
 * can publish such a file, so that is the advice.
 */
export function WorktreeImage({ path, alt = '', linked = false }: Props) {
  const actions = useFileRefActions()
  const rel = actions ? worktreeRelativePath(path, actions.cwd) : null
  if (!actions || rel === null) {
    const where = linked ? <code>{path}</code> : <CopyablePath text={path} />
    return (
      <span className={IMAGE_NOTICE_CLASS}>
        {alt || 'Image'} is {actions
          ? <>at {where} on the session host, outside its worktree,</>
          : <>a file on the session host ({where}),</>} so it can't be shown here — ask the session to share it with share_artifact.
      </span>
    )
  }
  return <InWorktreeImage actions={actions} rel={rel} path={path} alt={alt} linked={linked} />
}

function InWorktreeImage({ actions, rel, path, alt, linked }: Required<Props> & { actions: FileRefActions; rel: string }) {
  const file = useSessionFile(actions.sessionId, rel)
  if (file.isPending) return <span className={IMAGE_NOTICE_CLASS}>Loading image…</span>
  const image = file.data?.image
  if (!image || !RASTER.test(image.mediaType)) {
    return <span className={IMAGE_NOTICE_CLASS}>Image unavailable: <code>{path}</code> could not be read from the worktree as an image.</span>
  }
  const img = <img src={`data:${image.mediaType};base64,${image.data}`} alt={alt || path} className={RESULT_IMAGE_CLASS} />
  if (linked) return img
  return (
    <button
      type="button"
      title="Open in Files"
      aria-label={`Open ${path} in Files`}
      onClick={() => actions.openFile(rel, null)}
      className="block w-fit max-w-full cursor-pointer rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
    >
      {img}
    </button>
  )
}
