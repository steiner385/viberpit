interface Props {
  to: string
}

export function ModelSwitchDivider({ to }: Props) {
  const text = `⇄ model → ${to}`
  return (
    <div
      role="separator"
      aria-label={`Model switched to ${to}`}
      className="flex items-center gap-2 my-2 px-2 text-xs text-gray-500 dark:text-gray-400"
    >
      <div className="flex-1 border-t border-gray-200 dark:border-gray-700" />
      <span>{text}</span>
      <div className="flex-1 border-t border-gray-200 dark:border-gray-700" />
    </div>
  )
}
