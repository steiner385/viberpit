/** Portal target for overlays: #overlays (index.html) sits outside #root so
 *  `inert` on #root never disables the overlay itself. */
export function getOverlayRoot(): HTMLElement {
  return document.getElementById('overlays') ?? document.body
}
