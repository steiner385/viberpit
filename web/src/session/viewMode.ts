/**
 * Transcript view modes — Normal / Thinking / Verbose (Desktop parity, sub-spec D §4).
 *
 * A per-session, unpersisted lens over the user's transcript Settings: Normal is
 * exactly the Settings, Thinking opens every reasoning trace, Verbose also opens
 * every tool result. The single list and mapping live here; the runtime holds the
 * current mode and `ViewModeMenu` / Ctrl+⌘O only change it.
 */
import type { ToolDetailMode } from './groupBlocks'

/** Display (and Ctrl/⌘+O cycle) order. */
export const TRANSCRIPT_VIEW_MODES = [
  { value: 'normal', label: 'Normal' },
  { value: 'thinking', label: 'Thinking' },
  { value: 'verbose', label: 'Verbose' },
] as const satisfies readonly { value: string; label: string }[]

export type TranscriptViewMode = (typeof TRANSCRIPT_VIEW_MODES)[number]['value']

export function viewModeLabel(mode: TranscriptViewMode): string {
  return TRANSCRIPT_VIEW_MODES.find((m) => m.value === mode)?.label ?? mode
}

/** The mode after `mode` in cycle order, wrapping Verbose back to Normal. */
export function nextViewMode(mode: TranscriptViewMode): TranscriptViewMode {
  const i = TRANSCRIPT_VIEW_MODES.findIndex((m) => m.value === mode)
  return TRANSCRIPT_VIEW_MODES[(i + 1) % TRANSCRIPT_VIEW_MODES.length].value
}

export interface TranscriptViewSettings {
  defaultToolDetail: ToolDetailMode
  thinkingCollapsed: boolean
}

/** What a mode means for the transcript, given the user's Settings `s`. */
export function resolveViewMode(
  mode: TranscriptViewMode,
  s: TranscriptViewSettings,
): { toolDetailMode: ToolDetailMode; thinkingCollapsed: boolean } {
  switch (mode) {
    case 'normal':
      return { toolDetailMode: s.defaultToolDetail, thinkingCollapsed: s.thinkingCollapsed }
    case 'thinking':
      return { toolDetailMode: s.defaultToolDetail, thinkingCollapsed: false }
    case 'verbose':
      return { toolDetailMode: 'all-open', thinkingCollapsed: false }
  }
}
