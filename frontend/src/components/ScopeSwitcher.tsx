'use client';

import { useQuery } from '@tanstack/react-query';
import { getSites } from '@/services/api';
import { useAuth } from '@/context/AuthContext';
import { useScope } from '@/context/ScopeContext';
import type { Site } from '@/types/site';

/**
 * Topbar dropdown that lets the caller narrow every scope-aware list
 * page to a single site. The list of pickable sites is exactly what
 * ``getSites()`` returns — the backend already scopes that response to
 * what the caller can see — so the switcher is a lens on that
 * pre-filtered set, never a widening.
 *
 * Rendering rules:
 *   * No sites visible → nothing rendered.
 *   * Exactly one visible site → a read-only badge (nothing to switch
 *     between).
 *   * Two or more visible sites (or system-admin) → dropdown with
 *     ``All sites`` + one option per visible site.
 *
 * See docs/USER_PERMISSIONS_UX_REDESIGN.md §3.1.
 */
export function ScopeSwitcher() {
  const { user } = useAuth();
  const { selectedScope, setSelectedScope } = useScope();
  const { data: sites, isLoading } = useQuery<Site[]>({
    queryKey: ['sites'],
    queryFn: getSites,
  });

  if (isLoading) {
    return (
      <span className="text-xs text-white/60 px-3">Viewing: …</span>
    );
  }

  const visibleSites = sites ?? [];
  if (visibleSites.length === 0) return null;

  if (visibleSites.length === 1 && !user?.is_system_admin) {
    // Nothing to switch between — surface the current site as a badge
    // so the operator still knows what they're looking at.
    return (
      <span
        className="text-xs text-white/85 bg-white/10 rounded-md px-3 py-1"
        title="You have access to a single site — the switcher is inactive."
      >
        Viewing: {visibleSites[0].name}
      </span>
    );
  }

  const currentValue =
    selectedScope.kind === 'site' ? String(selectedScope.siteId) : 'all';

  const currentSiteMissing =
    selectedScope.kind === 'site' &&
    !visibleSites.some((s) => s.id === selectedScope.siteId);

  return (
    <label className="flex items-center gap-2 text-xs text-white/85">
      <span className="hidden sm:inline text-white/60">Viewing:</span>
      <select
        value={currentSiteMissing ? 'all' : currentValue}
        onChange={(e) => {
          const v = e.target.value;
          setSelectedScope(v === 'all' ? { kind: 'all' } : { kind: 'site', siteId: Number(v) });
        }}
        className="bg-white/10 text-white rounded-md px-2 py-1 text-xs border border-white/20 focus:outline-none focus:ring-2 focus:ring-white/40"
      >
        <option value="all">All sites</option>
        {visibleSites.map((s) => (
          <option key={s.id} value={s.id}>
            {s.name}
          </option>
        ))}
      </select>
    </label>
  );
}
