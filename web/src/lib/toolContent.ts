import type { ToolKind } from '@/session/events'

// Shared helpers for rendering `claude` tool-result payloads.
//
// Two realities these handle:
//  1. Tool output is full of terminal control bytes (colors, cursor moves, OSC
//     title sets, carriage-return progress redraws) that must be stripped before
//     display.
//  2. A tool_result's `content` is a UNION: usually a plain string, but sometimes
//     a list of content blocks (e.g. [{type:'text',text:'…'}], [{type:'tool_reference',…}]).
//     Renderers that assume a string fall back to dumping raw JSON at the user.

/* eslint-disable no-control-regex */
const CSI = /\x1b\[[0-9;?]*[ -/]*[@-~]/g // SGR colors AND other CSI (erase, cursor) sequences
const OSC = /\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g // OSC … BEL  or  OSC … ST
/* eslint-enable no-control-regex */

/** Strip ANSI escape sequences and resolve carriage-return overwrites. */
export function stripAnsi(s: string): string {
  const cleaned = s.replace(OSC, '').replace(CSI, '')
  // A bare \r rewinds to column 0; emulate by keeping only the final segment of
  // each line (what a terminal would actually show after progress redraws).
  return cleaned
    .split('\n')
    .map((line) => {
      const i = line.lastIndexOf('\r')
      return i === -1 ? line : line.slice(i + 1)
    })
    .join('\n')
}

interface ContentBlock {
  type?: string
  text?: string
}

/**
 * Normalize a tool_result `content` value to displayable text. Strings pass
 * through; arrays of content blocks have their text joined, with non-text blocks
 * labelled (`[image]`, `[tool_reference]`) rather than serialized as JSON.
 */
export function coerceText(content: unknown): string {
  if (typeof content === 'string') return content
  if (Array.isArray(content)) {
    return content
      .map((block) => {
        const b = block as ContentBlock
        if (b && b.type === 'text' && typeof b.text === 'string') return b.text
        if (b && typeof b.type === 'string') return `[${b.type}]`
        return ''
      })
      .filter((s) => s !== '')
      .join('\n')
  }
  if (content == null) return ''
  return JSON.stringify(content, null, 2)
}

/**
 * Extract displayable text from a tool_result `content` value, or return null
 * when the value is structured data (a plain object, or an array whose entries
 * are not content blocks) that a structured renderer should handle instead of
 * dumping as JSON. Unlike `coerceText`, this NEVER falls back to JSON.stringify
 * — null is the signal to render structurally.
 */
export function extractText(content: unknown): string | null {
  if (typeof content === 'string') return content
  if (Array.isArray(content)) {
    // Treat as a content-block array only if every entry looks like a block
    // ({ type: string }); otherwise it's structured data (e.g. search rows).
    const looksLikeBlocks = content.every(
      (b) => b != null && typeof b === 'object' && typeof (b as ContentBlock).type === 'string',
    )
    return looksLikeBlocks ? coerceText(content) : null
  }
  return null
}

/**
 * Build a unified-diff line list for an Edit/Write. The diff data lives in the
 * tool's `input` (Edit: old_string/new_string, Write: content) — the result
 * payload only carries a success/error message, so it must NOT be used here.
 */
export function buildLines(input: Record<string, unknown>): Array<{ prefix: '+' | '-'; text: string }> {
  const lines: Array<{ prefix: '+' | '-'; text: string }> = []
  const oldStr = typeof input.old_string === 'string' ? input.old_string : null
  const newStr = typeof input.new_string === 'string' ? input.new_string : null
  const content = typeof input.content === 'string' ? input.content : null

  if (oldStr !== null) {
    for (const line of oldStr.split('\n')) lines.push({ prefix: '-', text: line })
  }
  if (newStr !== null) {
    for (const line of newStr.split('\n')) lines.push({ prefix: '+', text: line })
  } else if (content !== null && oldStr === null) {
    // Write-only: content as added lines.
    for (const line of content.split('\n')) lines.push({ prefix: '+', text: line })
  }
  return lines
}

/**
 * Derive a size hint string (e.g. "42 lines", "+3 −2"). Edit/Write churn comes
 * from the tool `input` (where old_string/new_string live); read/bash line counts
 * come from the result `payload`.
 */
export function sizeHint(
  kind: ToolKind,
  input: Record<string, unknown>,
  payload: Record<string, unknown>,
): string {
  if (kind === 'edit' || kind === 'write') {
    const oldLines =
      typeof input.old_string === 'string' ? input.old_string.split('\n').length : 0
    const newLines =
      typeof input.new_string === 'string'
        ? input.new_string.split('\n').length
        : typeof input.content === 'string'
          ? input.content.split('\n').length
          : 0
    if (oldLines === 0 && newLines > 0) return `+${newLines}`
    if (newLines === 0 && oldLines > 0) return `−${oldLines}`
    return `+${newLines} −${oldLines}`
  }

  // For bash and read: count lines in the text content (content may be a string
  // OR a list of content blocks — coerceText handles both).
  let text: string | null = null
  if (payload.content != null) {
    text = coerceText(payload.content)
  } else if (typeof payload.stdout === 'string' || typeof payload.stderr === 'string') {
    const parts: string[] = []
    if (typeof payload.stdout === 'string' && payload.stdout) parts.push(payload.stdout)
    if (typeof payload.stderr === 'string' && payload.stderr) parts.push(payload.stderr)
    text = parts.join('\n')
  }
  if (text !== null) {
    const count = text === '' ? 0 : text.split('\n').length
    return `${count} ${count === 1 ? 'line' : 'lines'}`
  }

  return ''
}
