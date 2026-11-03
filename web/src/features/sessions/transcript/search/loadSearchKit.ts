/**
 * The one import expression naming the search kit's chunk (D-11) — the lazy bar and
 * the kit's renderers/jump both load through it, so they can never drift into two
 * chunks (the same rule as `markdownChunk`).
 */
export const loadSearchKit = () => import('./searchKit')

export type SearchKitModule = Awaited<ReturnType<typeof loadSearchKit>>
