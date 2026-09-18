import { useEffect, useState } from 'react'

let nextDiagramId = 0
// Mermaid's API queues renders internally. Initialize once, and load its sizeable
// renderer only when a completed diagram is actually mounted.
let renderer: Promise<typeof import('mermaid')['default']> | undefined
function loadRenderer() {
  renderer ??= import('mermaid').then(({ default: mermaid }) => {
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: 'strict',
      suppressErrorRendering: true,
      htmlLabels: false,
      secure: ['secure', 'securityLevel', 'startOnLoad', 'maxTextSize', 'maxEdges', 'suppressErrorRendering', 'htmlLabels'],
    })
    return mermaid
  }).catch((error: unknown) => {
    renderer = undefined
    throw error
  })
  return renderer
}

export function MermaidDiagram({ source }: { source: string }) {
  const [result, setResult] = useState<{ source: string; src?: string }>()
  useEffect(() => {
    let cancelled = false
    async function render() {
      // The temporary container belongs to this attempt, not to React. Always
      // remove it, including after a parse failure or unmount during rendering.
      const container = document.createElement('div')
      container.style.position = 'absolute'
      container.style.left = '-100000px'
      container.setAttribute('aria-hidden', 'true')
      try {
        const mermaid = await loadRenderer()
        if (cancelled) return
        document.body.append(container)
        const { svg } = await mermaid.render(`mermaid-diagram-${++nextDiagramId}`, source, container)
        if (!cancelled) setResult({ source, src: `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}` })
      } catch {
        if (!cancelled) setResult({ source })
      } finally {
        container.remove()
      }
    }
    void render()
    return () => { cancelled = true }
  }, [source])

  const current = result?.source === source ? result : undefined
  return (
    <figure className="mermaid-diagram" aria-label="Mermaid diagram">
      {/* An image isolates the generated SVG from the app DOM: diagram scripts,
          event handlers, links, and external resources cannot become active. */}
      {current?.src && <img src={current.src} alt="Mermaid diagram" />}
      {!current && <p role="status">Rendering diagram…</p>}
      {current && !current.src && <p role="status">Unable to render diagram. Source is shown below.</p>}
      <details open={!current?.src}>
        <summary>Diagram source</summary>
        <pre><code>{source}</code></pre>
      </details>
    </figure>
  )
}
