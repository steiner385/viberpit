import { useEffect, useRef, useState } from 'react'
import { copyText } from '@/lib/copyText'

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
      className="cursor-pointer rounded px-0.5 underline decoration-dotted underline-offset-2 hover:bg-surface-raised"
    >
      {text}
    </code>
  )
}
