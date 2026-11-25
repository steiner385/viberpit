import { toolResultStatus, type ToolCallStatus } from '@/lib/toolContent'
import type { Block } from './reducer'

/**
 * The Claude Code CLI's user-facing tools — calls whose ENTIRE purpose is to show
 * the user something: deliver files, a message, a code-review findings list, a
 * notification. Claude Desktop / claude.ai render them as first-class UI; here
 * they are promoted out of tool runs like `share_artifact` (see `groupBlocks`)
 * and rendered by `UserFacingToolCard`. A collapsed "SendUserFile" row is how a
 * delivered file went unseen while the CLI told the agent "1 file delivered".
 *
 * Built-in CLI tools: `toolKind` is `unknown` on the wire, so they are matched
 * by name. The input shapes come from the installed CLI's schemas; every parser
 * here is total — a malformed input degrades to an empty field, never a throw.
 */

export const USER_FACING_TOOLS = {
  SendUserFile: 'Send file',
  SendUserMessage: 'Message',
  ReportFindings: 'Findings',
  PushNotification: 'Notification',
} as const

export type UserFacingToolName = keyof typeof USER_FACING_TOOLS

export function isUserFacingToolName(name: string): name is UserFacingToolName {
  return Object.prototype.hasOwnProperty.call(USER_FACING_TOOLS, name)
}

/** The tool a block is, when it is one of the user-facing tools; else null. */
export function userFacingToolName(block: Block): UserFacingToolName | null {
  return block.kind === 'tool' && isUserFacingToolName(block.name) ? block.name : null
}

/** A human label for the tool row / card header; null for any other tool. */
export function userFacingDisplayName(name: string): string | null {
  return isUserFacingToolName(name) ? USER_FACING_TOOLS[name] : null
}

const MAX_SUMMARY_CHARS = 60

function clip(text: string): string {
  const line = text.split('\n', 1)[0].trim()
  return line.length > MAX_SUMMARY_CHARS ? `${line.slice(0, MAX_SUMMARY_CHARS)}…` : line
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : ''
}

/** Non-empty strings out of a would-be string array; [] for any other shape. */
function stringList(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string' && x.trim() !== '') : []
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? '' : 's'}`
}

/** The one-line gist for `toolSummary` — '' when the input says nothing usable. */
export function userFacingToolSummary(name: UserFacingToolName, input: Record<string, unknown>): string {
  switch (name) {
    case 'SendUserFile': {
      const { files, caption } = sentFilesFromInput(input)
      if (caption) return clip(caption)
      if (files.length === 1) return files[0]
      return files.length > 1 ? plural(files.length, 'file') : ''
    }
    case 'SendUserMessage':
      return clip(userMessageFromInput(input).message ?? '')
    case 'ReportFindings': {
      const { findings, malformed } = findingsFromInput(input)
      if (malformed) return ''
      return findings.length === 0 ? 'no findings' : plural(findings.length, 'finding')
    }
    case 'PushNotification':
      return clip(notificationFromInput(input))
  }
}

// ── PushNotification ──────────────────────────────────────────────────────────

/** There is no terminal to ring in ViberPit — the notice shows in the transcript. */
export function notificationFromInput(input: Record<string, unknown>): string {
  return str(input.message).trim()
}

// ── SendUserFile ──────────────────────────────────────────────────────────────

export interface SentFiles {
  /** As written: absolute, or relative to the session's cwd. */
  files: string[]
  caption: string | null
  /** `attach`: offered as files, not rendered inline. */
  display: 'render' | 'attach'
}

export function sentFilesFromInput(input: Record<string, unknown>): SentFiles {
  return {
    files: stringList(input.files),
    caption: str(input.caption).trim() || null,
    display: input.display === 'attach' ? 'attach' : 'render',
  }
}

// ── SendUserMessage ───────────────────────────────────────────────────────────

export interface UserMessage {
  message: string | null
  attachments: string[]
}

/** Schema unverified: `message` only when a string; unknown fields ignored. */
export function userMessageFromInput(input: Record<string, unknown>): UserMessage {
  const message = str(input.message).trim() ? str(input.message) : null
  return { message, attachments: stringList(input.attachments) }
}

// ── ReportFindings ────────────────────────────────────────────────────────────

export type FindingSeverity = 'important' | 'pre_existing' | 'nit'

export interface Finding {
  file: string
  line: number | null
  summary: string
  failureScenario: string
  severity: FindingSeverity | null
  shortSummary: string | null
  category: string | null
  verdict: string | null
  outcome: string | null
}

export interface Findings {
  findings: Finding[]
  level: string | null
  /** `findings` was not an array: say nothing rather than "no findings". */
  malformed: boolean
}

const SEVERITIES: ReadonlySet<string> = new Set<FindingSeverity>(['important', 'pre_existing', 'nit'])

function lineOf(v: unknown): number | null {
  const n = typeof v === 'string' ? Number(v) : v
  return typeof n === 'number' && Number.isInteger(n) && n > 0 ? n : null
}

export function findingsFromInput(input: Record<string, unknown>): Findings {
  const level = str(input.level).trim() || null
  if (!Array.isArray(input.findings)) return { findings: [], level, malformed: true }
  const findings = input.findings
    .filter((f): f is Record<string, unknown> => typeof f === 'object' && f !== null)
    .map((f) => ({
      file: str(f.file),
      line: lineOf(f.line),
      summary: str(f.summary),
      failureScenario: str(f.failure_scenario),
      severity: SEVERITIES.has(str(f.severity)) ? (str(f.severity) as FindingSeverity) : null,
      shortSummary: str(f.short_summary) || null,
      category: str(f.category) || null,
      verdict: str(f.verdict) || null,
      outcome: str(f.outcome) || null,
    }))
  return { findings, level, malformed: false }
}

// ── View ──────────────────────────────────────────────────────────────────────

/**
 * Everything a card needs, parsed on the eager side: the card bodies are a lazy
 * chunk, and every eager module they import is split into a chunk of its own
 * that the initial load then preloads (`npm run budget`). So they take this.
 */
export type UserFacingView = { call: ToolCallStatus } & (
  | { name: 'SendUserFile'; sent: SentFiles }
  | { name: 'SendUserMessage'; message: UserMessage }
  | { name: 'ReportFindings'; findings: Findings }
  | { name: 'PushNotification'; message: string }
)

export function userFacingView(name: UserFacingToolName, block: Extract<Block, { kind: 'tool' }>): UserFacingView {
  const call = toolResultStatus(block.result, `${name} failed`)
  const input = block.input ?? {}
  switch (name) {
    case 'SendUserFile': return { name, call, sent: sentFilesFromInput(input) }
    case 'SendUserMessage': return { name, call, message: userMessageFromInput(input) }
    case 'ReportFindings': return { name, call, findings: findingsFromInput(input) }
    case 'PushNotification': return { name, call, message: notificationFromInput(input) }
  }
}
