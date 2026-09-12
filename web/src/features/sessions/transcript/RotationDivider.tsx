interface Props {
  from: string
  to: string
  reason?: string
}

export function RotationDivider({ from, to, reason }: Props) {
  const label = `Account switched from ${from} to ${to}`
  const text = `↻ switched ${from} → ${to}${reason ? ` — ${reason}` : ''}`

  return (
    <div
      role="separator"
      aria-label={label}
      className="flex items-center gap-2 my-2 px-2 text-xs text-ink-muted"
    >
      <div className="flex-1 border-t border-edge" />
      <span>{text}</span>
      <div className="flex-1 border-t border-edge" />
    </div>
  )
}
