// The Roadmap section's pages that ship as ONE lazy chunk (`shell/routes.tsx`).
//
// The Milestones page (spec 2026-11-05 §9) is reached from a product's Overview,
// so it rides in the Roadmap chunk rather than in a lazy chunk of its own. As its
// own dynamic import it changed which lazy chunks share the entry's modules, and
// the bundler re-cut the PRELOADED chunks around that: +0.9 kB gzip on every
// first paint (`npm run budget`'s initialJs) for a page few loads visit. Sharing
// the Roadmap chunk costs the Roadmap a few kB instead, off the critical path.
export { RoadmapPage } from './RoadmapPage'
export { MilestonesPage } from '@/features/milestones/MilestonesPage'
