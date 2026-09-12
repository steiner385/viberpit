interface Props {
  to: string
}

export function ModelSwitchDivider({ to }: Props) {
  const text = `⇄ model → ${to}`
  return (
    <div
      role="separator"
      aria-label={`Model switched to ${to}`}
      className="flex items-center gap-2 my-2 px-2 text-xs text-ink-muted"
    >
      <div className="flex-1 border-t border-edge" />
      <span>{text}</span>
      <div className="flex-1 border-t border-edge" />
    </div>
  )
}
