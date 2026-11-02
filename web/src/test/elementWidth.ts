/**
 * Test seam for `useContainerMinWidth`: jsdom does no layout (`test/setup.ts`
 * pins every `offsetWidth` to 800) and has no ResizeObserver. `setElementWidth`
 * makes every element report `px` and swaps in a ResizeObserver that records
 * its observers, so `{ notify: true }` can fire them as a real resize would.
 * Returns the reset, which restores the setup's 800px getter and observer.
 */
type Callback = ResizeObserverCallback

const live = new Set<RecordingObserver>()

class RecordingObserver {
  private readonly cb: Callback
  constructor(cb: Callback) { this.cb = cb }
  observe() { live.add(this) }
  unobserve() { live.delete(this) }
  disconnect() { live.delete(this) }
  fire() { this.cb([], this as unknown as ResizeObserver) }
}

let originals: { width: PropertyDescriptor | undefined; RO: typeof ResizeObserver } | null = null

export function resetElementWidth(): void {
  if (!originals) return
  if (originals.width) Object.defineProperty(HTMLElement.prototype, 'offsetWidth', originals.width)
  globalThis.ResizeObserver = originals.RO
  live.clear()
  originals = null
}

export function setElementWidth(px: number, { notify = false }: { notify?: boolean } = {}): () => void {
  if (!originals) {
    originals = {
      width: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetWidth'),
      RO: globalThis.ResizeObserver,
    }
    globalThis.ResizeObserver = RecordingObserver as unknown as typeof ResizeObserver
  }
  Object.defineProperty(HTMLElement.prototype, 'offsetWidth', { configurable: true, get: () => px })
  if (notify) for (const o of [...live]) o.fire()
  return resetElementWidth
}
