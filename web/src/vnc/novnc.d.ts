declare module '@novnc/novnc' {
  /** Minimal surface of noVNC's RFB that VncClient uses (noVNC ships no types). */
  interface RFBInstance {
    scaleViewport: boolean
    addEventListener(
      type: string,
      listener: (e: Event & { detail?: { clean?: boolean; code?: number } }) => void,
    ): void
    focus(): void
    blur(): void
    disconnect(): void
  }
  const RFB: {
    new (target: Element, url: string, options?: Record<string, unknown>): RFBInstance
  }
  export default RFB
}
