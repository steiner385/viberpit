import { describe, it, expect } from 'vitest'
import { ApiRequestError } from '@/api/client'
import { MUTATION_ERROR_FALLBACK } from '@/api/queryClient'
import {
  describeCreateSessionFailure, REAUTH_MESSAGE, CREATE_SESSION_FALLBACK,
  ENGINE_UNAVAILABLE_FALLBACK, MODEL_EFFORT_MISMATCH_FALLBACK, MODELS_UNAVAILABLE_MESSAGE,
} from './createSessionFailure'

describe('describeCreateSessionFailure — the 409 triage', () => {
  it('branch_switch_failed keeps the server message', () => {
    const err = new ApiRequestError(409, { error: { code: 'branch_switch_failed', message: 'no feat/x' } })
    expect(describeCreateSessionFailure(err)).toEqual({ kind: 'branch', message: 'no feat/x' })
  })

  it('branch_switch_failed without a message gets a fixed line', () => {
    const err = new ApiRequestError(409, { error: { code: 'branch_switch_failed' } })
    expect(describeCreateSessionFailure(err)).toEqual({ kind: 'branch', message: 'Could not switch branch' })
  })

  it('accounts_need_reauth points at Settings → Accounts', () => {
    const err = new ApiRequestError(409, { error: { code: 'accounts_need_reauth' } })
    expect(describeCreateSessionFailure(err)).toEqual({ kind: 'reauth', message: REAUTH_MESSAGE })
  })

  it('all_accounts_limited carries the reset time when the daemon gave one', () => {
    expect(describeCreateSessionFailure(
      new ApiRequestError(409, { error: { code: 'all_accounts_limited' }, earliestReset: '2026-09-04T12:00:00Z' }),
    )).toEqual({ kind: 'all-limited', earliestReset: '2026-09-04T12:00:00Z' })
    expect(describeCreateSessionFailure(
      new ApiRequestError(409, { error: { code: 'all_accounts_limited' } }),
    ))
      .toEqual({ kind: 'all-limited', earliestReset: null })
  })

  it('does not hide an unclassified daemon 409 behind an empty all-limited banner', () => {
    expect(describeCreateSessionFailure(
      new ApiRequestError(409, { detail: 'credential authority refused account: account_disabled' }),
    )).toEqual({
      kind: 'other',
      message: 'credential authority refused account: account_disabled',
    })
  })

  it('a 409 whose body is not an object uses the create fallback (no throw)', () => {
    expect(describeCreateSessionFailure(new ApiRequestError(409, null as unknown as Record<string, unknown>)))
      .toEqual({ kind: 'other', message: CREATE_SESSION_FALLBACK })
  })
})

describe('describeCreateSessionFailure — everything else', () => {
  it('uses the body message when the server sent one', () => {
    const err = new ApiRequestError(500, { detail: { message: 'daemon is draining' } })
    expect(describeCreateSessionFailure(err)).toEqual({ kind: 'other', message: 'daemon is draining' })
  })

  it('falls back to a create-specific line, not the generic mutation fallback', () => {
    const got = describeCreateSessionFailure(new ApiRequestError(500, {}))
    expect(got).toEqual({ kind: 'other', message: CREATE_SESSION_FALLBACK })
    expect(got).not.toEqual({ kind: 'other', message: MUTATION_ERROR_FALLBACK })
  })

  it('a non-API error (network) also lands on the fallback', () => {
    expect(describeCreateSessionFailure(new TypeError('Failed to fetch')))
      .toEqual({ kind: 'other', message: CREATE_SESSION_FALLBACK })
  })
})

describe('describeCreateSessionFailure — the engine-aware failures', () => {
  it('engine_unavailable says so, and keeps the server line naming the engine', () => {
    const err = new ApiRequestError(409, {
      error: { code: 'engine_unavailable', message: 'agent engine is not available: codex', engine: 'codex' },
    })
    expect(describeCreateSessionFailure(err)).toEqual({
      kind: 'engine', message: 'agent engine is not available: codex', hostReasons: {},
    })
  })

  it('no_engine_host_available is an ENGINE failure, not "all accounts limited"', () => {
    // Same 409 status as the usage ceiling, and `NoEngineHostAvailable` even
    // subclasses `NoHostAvailable` — so without its own branch this lands in the
    // all-limited banner and sends the operator to look at account quotas
    // instead of at a missing Codex binary. `hostReasons` names which to fix.
    const err = new ApiRequestError(409, {
      error: {
        code: 'no_engine_host_available',
        message: 'no healthy host can run engine codex (rocky: binary_not_found)',
        engine: 'codex',
        hostReasons: { rocky: 'binary_not_found', edge: 'contract_hash_mismatch' },
      },
    })
    expect(describeCreateSessionFailure(err)).toEqual({
      kind: 'engine',
      message: 'no healthy host can run engine codex (rocky: binary_not_found)',
      hostReasons: { rocky: 'binary_not_found', edge: 'contract_hash_mismatch' },
    })
  })

  it('a plain "no host available" 409 stays beside the dispatch form', () => {
    // This is runner availability, not an account quota. Hiding it behind the
    // quota banner leaves no message when there is no quota reset timestamp.
    const err = new ApiRequestError(409, {
      error: { code: 'no_host_available', message: 'no host available' },
    })
    expect(describeCreateSessionFailure(err)).toEqual({ kind: 'other', message: 'no host available' })
  })

  it('codex_models_unavailable (502 from the app-server) is an engine failure', () => {
    const err = new ApiRequestError(502, {
      detail: { error: 'codex_models_unavailable', message: 'model/list failed' },
    })
    expect(describeCreateSessionFailure(err)).toEqual({
      kind: 'engine', message: MODELS_UNAVAILABLE_MESSAGE, hostReasons: {},
    })
  })

  it('model_effort_mismatch is its own kind and keeps the rejected pair', () => {
    const err = new ApiRequestError(409, {
      error: {
        error: 'model_effort_mismatch', model: 'gpt-5-codex', effort: 'ultra',
        choices: [{ id: 'gpt-5-codex', reasoningEfforts: ['low', 'high'] }],
      },
    })
    expect(describeCreateSessionFailure(err)).toEqual({
      kind: 'model',
      message: MODEL_EFFORT_MISMATCH_FALLBACK,
      model: 'gpt-5-codex',
      effort: 'ultra',
    })
  })

  it('model_effort_mismatch without the pair still reads as a model failure', () => {
    const err = new ApiRequestError(409, { error: { code: 'model_effort_mismatch' } })
    expect(describeCreateSessionFailure(err)).toEqual({
      kind: 'model', message: MODEL_EFFORT_MISMATCH_FALLBACK, model: null, effort: null,
    })
  })

  it('engine_unavailable with no message falls back to a fixed line', () => {
    const err = new ApiRequestError(409, { error: { code: 'engine_unavailable' } })
    expect(describeCreateSessionFailure(err)).toEqual({
      kind: 'engine', message: ENGINE_UNAVAILABLE_FALLBACK, hostReasons: {},
    })
  })
})
