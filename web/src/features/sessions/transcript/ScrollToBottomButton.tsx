/**
 * The floating "Scroll to bottom" pill shown over a transcript the user has
 * scrolled up in. Position it inside a `relative` box that wraps the scroller;
 * pair it with `useStickToBottom`, which owns when it shows and what a click does.
 */
export function ScrollToBottomButton({ onClick }: { onClick: () => void }) {
  return (
    <div className="absolute bottom-4 left-0 right-0 flex justify-center pointer-events-none">
      <button
        type="button"
        onClick={onClick}
        aria-label="Scroll to bottom"
        className="pointer-events-auto flex items-center gap-1.5 rounded-full border border-edge bg-surface px-3 py-1.5 text-xs font-medium text-ink-muted shadow-md transition hover:bg-surface-raised hover:shadow-lg"
      >
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true" className="size-3.5">
          <path fillRule="evenodd" d="M8 1a.75.75 0 0 1 .75.75v8.69l2.72-2.72a.75.75 0 1 1 1.06 1.06l-4 4a.75.75 0 0 1-1.06 0l-4-4a.75.75 0 0 1 1.06-1.06l2.72 2.72V1.75A.75.75 0 0 1 8 1Z" clipRule="evenodd" />
        </svg>
        Scroll to bottom
      </button>
    </div>
  )
}
