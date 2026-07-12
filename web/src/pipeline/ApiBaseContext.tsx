// Ported from plotroom@5166b52 frontend/src/embed/ApiBaseContext.tsx; edits: eslint-disable react-refresh/only-export-components (ported file mixes component and non-component exports)
/* eslint-disable react-refresh/only-export-components */
import { createContext, useContext, useMemo, type ReactNode } from 'react';

/** Join an API base and a path with exactly one separating slash. */
export function joinApiUrl(base: string, path: string): string {
  const b = base.replace(/\/+$/, '');
  const p = path.startsWith('/') ? path : `/${path}`;
  return `${b}${p}`;
}

export interface ApiBase {
  base: string;
  apiUrl: (path: string) => string;
  fetch: typeof fetch;
  withCredentials: boolean;
}

const DEFAULT_BASE = '/api';
const defaultValue: ApiBase = {
  base: DEFAULT_BASE,
  apiUrl: (p) => joinApiUrl(DEFAULT_BASE, p),
  fetch: (...a: Parameters<typeof fetch>) => fetch(...a),
  withCredentials: false,
};

const ApiBaseContext = createContext<ApiBase>(defaultValue);

export function ApiBaseProvider(
  { base = DEFAULT_BASE, withCredentials = false, fetchImpl, children }:
  { base?: string; withCredentials?: boolean; fetchImpl?: typeof fetch; children: ReactNode },
) {
  const value = useMemo<ApiBase>(() => ({
    base,
    apiUrl: (p) => joinApiUrl(base, p),
    fetch: fetchImpl ?? ((...a: Parameters<typeof fetch>) => fetch(...a)),
    withCredentials,
  }), [base, withCredentials, fetchImpl]);
  return <ApiBaseContext.Provider value={value}>{children}</ApiBaseContext.Provider>;
}

export function useApiBase(): ApiBase {
  return useContext(ApiBaseContext);
}
