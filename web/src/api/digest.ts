// Owner digest (design 2026-10-14 §5) types, fetchers and hooks. Kept out of roadmap.ts and
// hooks.ts so they ride the lazy Roadmap chunk instead of the critical-path bundle (bundle budget).
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api, type ApiRequestError } from './client'
import type { HeldAttentionKind } from './roadmap'
import { PM_MUTATION_META, QK } from './hooks'

/** What a digest item asks of a person (`server/pm/digest.py:KINDS`): the held-issue
 *  kinds (`HeldAttentionKind`), an owner recommendation the needs-you hygiene loop
 *  left, a triage proposal, an initiative gate, or another needs-you issue. */
export type DigestItemKind = HeldAttentionKind | 'recommendation' | 'triage' | 'gate' | 'issue'

/** The ONE action the digest recommends for an item; `label` is the button text. */
export interface DigestRecommendation {
  action: 'approve_escalation' | 'apply_recommendation' | 'accept_triage'
    | 'accept_decision' | 'advance_gate' | 'release_hold'
  label: string
  params: Record<string, unknown>
}

export interface DigestItem {
  id: string
  kind: DigestItemKind
  product: string
  ref: string | null
  /** An initiative gate's slug. */
  slug: string | null
  title: string
  why: string
  /** A GitHub url, or an app path (starts with `/`) for a gate. */
  url: string | null
  /** Null when only a person can decide (a decision, a checklist, a gate). */
  recommendation: DigestRecommendation | null
  /** Evidence links — the merged PRs a parked review read. */
  evidence: { title: string; url: string }[]
}

/** `GET /pm/digest` (design 2026-10-14 §5): every needs-you item, grouped by
 *  product, each with at most one recommended action. */
export interface OwnerDigest {
  generatedAt: string
  /** `ownerEvidence` (#3113): the product's `owner_evidence` asks — evidence only
   *  the owner can supply, answered on the issue — apart from what needs deciding. */
  products: { slug: string; items: DigestItem[]; ownerEvidence?: DigestItem[] }[]
  total: number
  withRecommendation: number
}

export function fetchOwnerDigest(product?: string): Promise<OwnerDigest> {
  const q = product ? `?product=${encodeURIComponent(product)}` : ''
  return api.get<OwnerDigest | unknown[]>(`/pm/digest${q}`).then((v) =>
    // The e2e catch-all answers `[]` for any unstubbed collection; read it as empty.
    Array.isArray(v) ? { generatedAt: '', products: [], total: 0, withRecommendation: 0 } : v)
}

export interface DigestApplyResult {
  results: { id: string; ok: boolean; step: string | null; error: string | null }[]
  applied: number
  failed: number
}

/** `POST /pm/digest/apply`: each id's recommended action, through the path that
 *  owns it. Always confirmed — the dialog that calls this lists every action. */
export function applyOwnerDigest(ids: string[]): Promise<DigestApplyResult> {
  return api.post<DigestApplyResult>('/pm/digest/apply', { ids, confirm: true })
}

/** Today's owner digest (design 2026-10-14 §5): every needs-you item with its one
 *  recommended action — one product, or all. */
export function useOwnerDigest(
  product?: string,
  { enabled = true }: { enabled?: boolean } = {},
) {
  return useQuery<OwnerDigest>({
    queryKey: [...QK.digest, product ?? null],
    queryFn: () => fetchOwnerDigest(product),
    enabled,
    staleTime: 5_000,
    refetchInterval: 60_000,
  })
}

/** Apply digest items' recommended actions (`POST /pm/digest/apply`). Each may
 *  release a hold, dispatch a session, close or unpark an issue or accept a triage
 *  proposal, so every view those touch is invalidated. Failures show inline. */
export function useApplyDigest() {
  const qc = useQueryClient()
  return useMutation<DigestApplyResult, ApiRequestError, string[]>({
    mutationFn: (ids) => applyOwnerDigest(ids),
    onSuccess: () => {
      for (const key of [QK.digest, QK.holds, QK.roadmapMap, QK.queue, QK.triage, ['pm', 'leaves']]) {
        void qc.invalidateQueries({ queryKey: key })
      }
      void qc.invalidateQueries({ queryKey: QK.automationSessions('backlog-runner') })
    },
    meta: PM_MUTATION_META,
  })
}

