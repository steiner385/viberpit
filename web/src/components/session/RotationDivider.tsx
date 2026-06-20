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
      className="flex items-center gap-2 my-2 px-2 text-xs text-gray-500 dark:text-gray-400"
    >
      <div className="flex-1 border-t border-gray-200 dark:border-gray-700" />
      <span>{text}</span>
      <div className="flex-1 border-t border-gray-200 dark:border-gray-700" />
    </div>
  )
}
