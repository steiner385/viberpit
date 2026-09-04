/**
 * Real react-query result objects for tests that mock `@/api/hooks`. Refs #952.
 *
 * The suites here stub hooks with a two-field literal and cast it at the hook's
 * return type — `{ data: [], isLoading: false } as ReturnType<typeof useSessions>`.
 * That cast is a lie the compiler can only shrug at (TS2352 when it notices,
 * silence when it does not): the stub is missing two dozen fields, so a
 * component that starts reading `status`, `isPending` or `error` gets
 * `undefined` in the test and something real in the browser — and the test
 * still passes.
 *
 * These builders return a COMPLETE, correctly-typed result, so no cast is
 * needed at the call site and the stub cannot drift from react-query's shape.
 * They are deliberately not `as unknown as` — if react-query adds a field,
 * this file is where it fails to compile.
 */
import type { UseMutationResult, UseQueryResult } from '@tanstack/react-query'

/** A settled, successful `useQuery` result holding `data`. */
export function queryResult<T>(
  data: T,
  over: Partial<UseQueryResult<T, Error>> = {},
): UseQueryResult<T, Error> {
  const result: UseQueryResult<T, Error> = {
    data,
    dataUpdatedAt: 0,
    error: null,
    errorUpdatedAt: 0,
    failureCount: 0,
    failureReason: null,
    errorUpdateCount: 0,
    isError: false,
    isFetched: true,
    isFetchedAfterMount: true,
    isFetching: false,
    isLoading: false,
    isPending: false,
    isLoadingError: false,
    isInitialLoading: false,
    isPaused: false,
    isPlaceholderData: false,
    isRefetchError: false,
    isRefetching: false,
    isStale: false,
    isSuccess: true,
    isEnabled: true,
    refetch: () => Promise.resolve(result),
    status: 'success',
    fetchStatus: 'idle',
    promise: Promise.resolve(data),
  }
  return Object.assign(result, over)
}

/**
 * A query that has never fetched and is not fetching — `data: undefined` with
 * `isLoading: false`. The real shape of a `useQuery` held behind `enabled:
 * false`, which is what the suites stubbing `{ data: undefined, isLoading:
 * false }` meant. `queryResult` cannot express it: a settled SUCCESS always
 * carries data.
 */
export function idleQueryResult<T>(
  over: Partial<UseQueryResult<T, Error>> = {},
): UseQueryResult<T, Error> {
  const result: UseQueryResult<T, Error> = {
    data: undefined,
    dataUpdatedAt: 0,
    error: null,
    errorUpdatedAt: 0,
    failureCount: 0,
    failureReason: null,
    errorUpdateCount: 0,
    isError: false,
    isFetched: false,
    isFetchedAfterMount: false,
    isFetching: false,
    isLoading: false,
    isPending: true,
    isLoadingError: false,
    isInitialLoading: false,
    isPaused: false,
    isPlaceholderData: false,
    isRefetchError: false,
    isRefetching: false,
    isStale: true,
    isSuccess: false,
    isEnabled: false,
    refetch: () => Promise.resolve(result),
    status: 'pending',
    fetchStatus: 'idle',
    promise: new Promise<T>(() => {}),
  }
  return Object.assign(result, over)
}

/**
 * A query fetching for the first time: `isLoading` true, no data yet. `data` is
 * `undefined` here rather than an empty array because that is what react-query
 * actually holds before the first response — an `isLoading: true` result that
 * also carries data is not a state the library can produce.
 */
export function loadingQueryResult<T>(): UseQueryResult<T, Error> {
  return idleQueryResult<T>({
    isLoading: true,
    isFetching: true,
    isEnabled: true,
    fetchStatus: 'fetching',
  })
}

/** An idle `useMutation` result; pass `{ mutate }` to supply the spy under test. */
export function mutationResult<TData, TError, TVariables, TOnMutateResult>(
  over: Partial<UseMutationResult<TData, TError, TVariables, TOnMutateResult>> = {},
): UseMutationResult<TData, TError, TVariables, TOnMutateResult> {
  const result: UseMutationResult<TData, TError, TVariables, TOnMutateResult> = {
    context: undefined,
    data: undefined,
    error: null,
    failureCount: 0,
    failureReason: null,
    isError: false,
    isIdle: true,
    isPending: false,
    isPaused: false,
    isSuccess: false,
    mutate: () => {},
    mutateAsync: () => Promise.reject(new Error('mutateAsync not stubbed')),
    reset: () => {},
    status: 'idle',
    submittedAt: 0,
    variables: undefined,
  }
  return Object.assign(result, over)
}
