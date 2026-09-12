'use client';

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
} from 'react';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';

/**
 * The active viewing scope in the dashboard shell. Selecting a site
 * narrows every scope-aware list page (currently just ``/users``, more
 * to follow per docs/USER_PERMISSIONS_UX_REDESIGN.md §3.1 / §3.7) to
 * that site's slice of what the caller can already see. It is a lens on
 * top of the caller's backend-enforced visibility, never a widening.
 */
export type SelectedScope =
  | { kind: 'all' }
  | { kind: 'site'; siteId: number };

interface ScopeContextValue {
  selectedScope: SelectedScope;
  setSelectedScope: (next: SelectedScope) => void;
}

const ScopeContext = createContext<ScopeContextValue | null>(null);

const QUERY_KEY = 'scope';
const STORAGE_KEY = 'ansiauth.scope';

function parseScope(raw: string | null): SelectedScope {
  if (!raw || raw === 'all') return { kind: 'all' };
  if (raw.startsWith('site:')) {
    const id = Number(raw.slice(5));
    if (Number.isFinite(id) && id > 0) return { kind: 'site', siteId: id };
  }
  return { kind: 'all' };
}

function stringifyScope(s: SelectedScope): string {
  return s.kind === 'all' ? 'all' : `site:${s.siteId}`;
}

/**
 * URL query param is the source of truth so links are shareable. On
 * first mount, if the URL has no ``scope`` param we hydrate from
 * localStorage (the last picked value in this browser) and reflect it
 * into the URL. Subsequent picks write to both.
 */
export function ScopeProvider({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const raw = searchParams.get(QUERY_KEY);
  const selectedScope = useMemo(() => parseScope(raw), [raw]);

  // Re-hydrate the URL from localStorage every time the URL loses the
  // ``scope`` param — not just on first mount. Navigating between
  // dashboard pages via <Link> drops query params by default, so a user
  // who picked "Site A" and then clicked the Audit icon would otherwise
  // land on ``/audit`` with no scope and revert to "all" silently.
  //
  // Idempotent — the effect only rewrites the URL when the URL is
  // missing ``scope`` AND localStorage has a meaningful (non-"all")
  // value, so a stable URL doesn't trigger a redraw loop.
  useEffect(() => {
    if (raw !== null) return;
    if (typeof window === 'undefined') return;
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (!stored || stored === 'all') return;
    const params = new URLSearchParams(searchParams.toString());
    params.set(QUERY_KEY, stored);
    router.replace(`${pathname}?${params.toString()}`, { scroll: false });
  }, [raw, pathname, router, searchParams]);

  const setSelectedScope = useCallback(
    (next: SelectedScope) => {
      const str = stringifyScope(next);
      if (typeof window !== 'undefined') {
        window.localStorage.setItem(STORAGE_KEY, str);
      }
      const params = new URLSearchParams(searchParams.toString());
      if (next.kind === 'all') {
        params.delete(QUERY_KEY);
      } else {
        params.set(QUERY_KEY, str);
      }
      const query = params.toString();
      router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
    },
    [pathname, router, searchParams],
  );

  const value = useMemo(
    () => ({ selectedScope, setSelectedScope }),
    [selectedScope, setSelectedScope],
  );

  return <ScopeContext.Provider value={value}>{children}</ScopeContext.Provider>;
}

export function useScope(): ScopeContextValue {
  const ctx = useContext(ScopeContext);
  if (ctx === null) {
    throw new Error('useScope must be used within a ScopeProvider');
  }
  return ctx;
}
