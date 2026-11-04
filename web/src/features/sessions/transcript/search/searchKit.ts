/**
 * The transcript search kit (D-11): everything ⌘F needs past the eager context —
 * the bar, the renderers and the jump. One module, so one lazy chunk: loaded the
 * first time someone searches (`loadSearchKit`), never on a plain page load.
 */
export { default as TranscriptSearchBar } from './TranscriptSearchBar'
export { renderKit } from './searchRender'
export { driveJump } from './searchJump'
export type { JumpRequest, PendingJump } from './searchJump'
