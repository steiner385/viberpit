// The palette's lazy entry point: `shell/regions/Overlays` reaches it only
// through `import('@/features/palette')`. Never import it statically from the
// shell — `paletteBoundary.test.ts` fails the build of that mistake.
export { AppPalette } from './AppPalette'
