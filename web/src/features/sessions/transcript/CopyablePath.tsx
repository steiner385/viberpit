import { useEffect, useRef, useState } from 'react'
import { copyText } from '@/lib/copyText'

/** `title`: what a click copies (default "Copy path"), e.g. a restore command. */
interface Props { text: string; title?: string }

// A transcript code span that looks like a file path. Clicking (or pressing
// Enter/Space) copies the path to the clipboard and flashes brief feedback —
// the tooltip for the pointer, a polite live region for a screen reader.
export function CopyablePath({ text, title = 'Copy path' }: Props) {
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
    <>
      <code
        role="button"
        tabIndex={0}
        title={copied ? 'Copied!' : title}
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
      <span className="sr-only" aria-live="polite">{copied ? 'Copied' : ''}</span>
    </>
  )
}
