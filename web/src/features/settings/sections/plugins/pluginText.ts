// Helpers for Settings › Plugins (D-16c). Everything a marketplace supplies (names,
// descriptions, sources) is untrusted: it is only ever rendered as React text, never as
// HTML or markdown, and anything shown verbatim is bounded here first.
import { ApiRequestError } from '@/api/client'
import { extractMutationErrorMessage } from '@/api/queryClient'
import type { LibraryComponentCounts, LibraryMarketplace, PluginComponentsSummary } from '@/api/types'

/** Owner decision D-16c consent copy — verbatim, do not edit. */
export const CONSENT_COPY =
  'Plugins run their own code — hooks and MCP servers — in every session on every host, including unattended automations. Install only plugins you trust.'
/** Shown after an install or update succeeds — verbatim. */
export const RESTART_COPY = 'Running sessions pick this up when they restart.'
export const RELOAD_LABEL = 'Also reload running sessions now'
export const RELOAD_HELP =
  'A session that has started talking and whose tools would change keeps its plugins until it restarts, so its prompt cache is kept. Sessions mid-turn are skipped.'
export const RELOAD_BUTTON = 'Reload running sessions'
export const NO_SESSIONS_COPY = 'No running session to reload.'
export const CONTENTS_UNKNOWN = 'Contents unknown — inspect the marketplace before installing'
/** A fan-out no host answered: never success. */
export const NO_HOSTS_COPY = 'No host answered — nothing was changed'
export const SOURCE_CHANGED_COPY = 'The source changed since this page loaded — review it again'
export const INSTALL_OFF_COPY =
  'Installing, updating and adding marketplaces is turned off on this server. Enable, disable, uninstall and remove still work.'
/** `GET /library/plugins` failed: never shown as an empty list. */
export const LOAD_FAILED_COPY = 'Couldn’t load installed plugins.'
/** The Anthropic Directory: entries from it reach the client only when reviewed. */
export const DIRECTORY_MARKETPLACE = 'anthropic-plugin-directory'

const MAX_SHOWN = 300

/** A bounded single string for an untrusted value. */
export function clip(text: string, max = MAX_SHOWN): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text
}

/**
 * The source to show for a marketplace: the server's `normalizedSource` verbatim (what a
 * Confirm echoes), else the raw `source` object as compact JSON — never interpreted.
 */
export function marketplaceSourceText(m: LibraryMarketplace | undefined): string | null {
  if (!m) return null
  if (typeof m.normalizedSource === 'string' && m.normalizedSource) return m.normalizedSource
  if (m.source == null) return null
  if (typeof m.source === 'string') return clip(m.source)
  try {
    return clip(JSON.stringify(m.source))
  } catch {
    return null
  }
}

type Counts = Partial<PluginComponentsSummary & LibraryComponentCounts>

const RUNNABLE: [keyof Counts, string][] = [
  ['hooks', 'hooks'],
  ['mcpServers', 'MCP servers'],
  ['lspServers', 'LSP servers'],
  ['agents', 'agents'],
  ['skills', 'skills'],
  ['commands', 'commands'],
]

/**
 * What a plugin can run, as "2 hooks · 1 MCP servers", or null when unknown. An
 * all-zero count is treated as unknown: the scan reports zeros for a plugin missing from
 * the catalog, and the consent card must never read "runs nothing" by accident.
 */
export function runnableSummary(c: Counts | null | undefined): string | null {
  if (!c) return null
  const parts = RUNNABLE
    .map(([key, label]) => [Number(c[key] ?? 0), label] as const)
    .filter(([n]) => Number.isFinite(n) && n > 0)
    .map(([n, label]) => `${n} ${label}`)
  return parts.length ? parts.join(' · ') : null
}

function detailOf(err: unknown): Record<string, unknown> | null {
  if (!(err instanceof ApiRequestError)) return null
  const d = err.body?.detail
  return d && typeof d === 'object' ? (d as Record<string, unknown>) : null
}

/** The refusal `reason` (`source_changed` | `not_confirmed`) on a policy 409. */
export function refusalReason(err: unknown): string | null {
  const r = detailOf(err)?.reason
  return typeof r === 'string' ? r : null
}

/** The kill switch flipped between page load and click: 404 `{error: plugin_install_disabled}`. */
export function isInstallDisabled(err: unknown): boolean {
  return err instanceof ApiRequestError && err.status === 404 && err.body?.error === 'plugin_install_disabled'
}

/** One line for a refused mutation. */
export function refusalMessage(err: unknown): string {
  if (isInstallDisabled(err)) return INSTALL_OFF_COPY
  if (refusalReason(err) === 'source_changed') return SOURCE_CHANGED_COPY
  return extractMutationErrorMessage(err, err instanceof Error && err.message ? err.message : undefined)
}
