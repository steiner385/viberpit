// Hide HTML comments from rendered transcript text.
//
// Neither of `AssistantText`'s two branches hides them on its own. The streaming
// branch prints text verbatim. The settled branch hands it to `Markdown`, which runs
// react-markdown WITHOUT `rehype-raw` — and that does not drop raw HTML, it escapes
// it, so `<!-- x -->` comes out as readable `&lt;!-- x --&gt;` text. The visible
// result is that the harness markers the agent appends to a final message
// (`<!-- viberpit:complete -->` and friends) sit at the end of every finished
// session's transcript.
//
// Stripping comments generically, rather than matching the marker literals, is
// deliberate: those literals have a single source of truth server-side
// (`server/sessions/completion.py`), and a second copy here would be one more place
// to update every time a marker is added, renamed, or migrated onto the session tool
// surface. Nothing the agent legitimately means to SHOW travels in an HTML comment.
const COMPLETED = /<!--[\s\S]*?-->/g

// A comment whose `-->` has not arrived yet. Without this the opening delimiter and
// the marker's first characters render for a frame or two as the deltas land.
const STILL_ARRIVING = /<!--(?:(?!-->)[\s\S])*$/

/** *text* with HTML comments removed, except inside fenced code blocks.
 *
 * A comment in a fence is content, not signalling — an agent editing an HTML file
 * quotes them constantly — and the reader is meant to see it. */
export function hideHtmlComments(text: string): string {
  if (!text) return text

  // Segments alternate outside/inside a fence, so the delimiter is captured rather
  // than discarded and the state flips as it is passed. An unclosed fence (mid-
  // stream) simply leaves the tail marked as inside, which is what it is.
  const parts = text.split(/(```)/)
  let inFence = false
  const stripped = parts
    .map((part, i) => {
      if (part === '```') {
        inFence = !inFence
        return part
      }
      if (inFence) return part
      const out = part.replace(COMPLETED, '')
      return i === parts.length - 1 ? out.replace(STILL_ARRIVING, '') : out
    })
    .join('')

  // Only when something was actually removed: a trailing newline left behind by a
  // stripped marker renders as a blank line, but trailing whitespace the agent is
  // still typing is real and must not be eaten.
  return stripped === text ? text : stripped.trimEnd()
}
