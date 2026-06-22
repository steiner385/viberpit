/**
 * Auto-grow a textarea: reset to natural height, then size to content,
 * capped at `maxPx`. Mirrors the inline resize logic used by the prompt
 * surfaces so dictated text grows the box the same way typing does.
 */
export function autoGrow(el: HTMLTextAreaElement | null, maxPx = 192): void {
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, maxPx)}px`
}
