import { useEffect, useRef, useState } from 'react'

// Best-effort clipboard copy: prefer the async Clipboard API, fall back to a
// hidden textarea + execCommand for insecure contexts / older mobile browsers.
// Never throws; resolves to whether the copy succeeded.
async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // fall through to the legacy path
  }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    return ok
  } catch {
    return false
  }
}

interface Props { text: string }

// A transcript code span that looks like a file path. Clicking (or pressing
// Enter/Space) copies the path to the clipboard and flashes brief feedback.
export function CopyablePath({ text }: Props) {
  const [copied, setCopied] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => () => { if (timer.current) clearTimeout(timer.current) }, [])

  async function onCopy() {
    const ok = await copyText(text)
    if (!ok) return
    setCopied(true)
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => setCopied(false), 1500)
  }

  return (
    <code
      role="button"
      tabIndex={0}
      title={copied ? 'Copied!' : 'Copy path'}
      onClick={onCopy}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          void onCopy()
        }
      }}
      className="cursor-pointer rounded px-0.5 underline decoration-dotted underline-offset-2 hover:bg-gray-200/60 dark:hover:bg-gray-700/60"
    >
      {text}
    </code>
  )
}
