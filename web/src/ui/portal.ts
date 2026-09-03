/**
 * The ONE portal target for overlays: `#overlays`, which `index.html` places
 * outside `#root` so `inert` on `#root` never disables the overlay itself.
 *
 * The node is created on first use when the document does not already carry it
 * — a jsdom test, or any host page that mounts the app without our
 * `index.html`. It used to fall back to `document.body` instead, which was
 * silent and wrong in two ways:
 *
 *   • `shell/regions/Overlays` stamps `data-region="app.overlays"` on whatever
 *     this returns. Stamping `<body>` would declare the entire document to be
 *     the overlay region, and `document.querySelector('[data-region=...]')`
 *     — how every region test finds its region — would start matching the page.
 *   • Overlays portaled straight into `<body>` sit as siblings of `#root`
 *     rather than in a node the shell owns, so nothing could style, order or
 *     inert them as a group.
 *
 * Creating is idempotent: the second call finds the node the first one made, so
 * there is exactly one overlay root per document however many times this runs.
 */
export function getOverlayRoot(): HTMLElement {
  const existing = document.getElementById('overlays')
  if (existing) return existing
  const created = document.createElement('div')
  created.id = 'overlays'
  document.body.appendChild(created)
  return created
}
