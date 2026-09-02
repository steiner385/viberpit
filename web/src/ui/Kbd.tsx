export function Kbd({ keys }: { keys: string }) {
  return (
    <kbd className="inline-block rounded-sm border border-edge-strong bg-surface-raised px-1.5 py-0.5 text-xs font-mono text-ink">
      {keys}
    </kbd>
  )
}
