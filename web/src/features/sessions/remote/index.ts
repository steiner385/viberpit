// The remote sessions feature's ONE lazy entry. The list's Remote group and the
// remote page are both dynamic-imported through this module so they share a single
// chunk: each extra dynamic entry that reaches eager modules makes the bundler split
// the initial graph into more preloaded common chunks (measured: two entries cost
// ~2.5 kB gzip more initialJs than one).
export { RemoteSessionsSection } from './RemoteSessionsSection'
export { RemoteSessionPage } from './RemoteSessionPage'
