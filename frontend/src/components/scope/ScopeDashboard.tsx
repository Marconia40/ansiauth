'use client';

import { useEffect, useMemo, useRef } from 'react';
import Link from 'next/link';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { getDashboardSummary, refreshDashboardScope } from '@/services/api';
import type { DashboardSummary, DashboardSummaryParams } from '@/types/dashboard';
import { vendorLabel } from '@/types/device';
import { Panel } from './Panel';
import { JobsPieChart, type JobsPieData } from './JobsPieChart';
import { Pie } from './Pie';
import { LastSyncedLabel } from './LastSyncedLabel';
import { SYNC_POLL_INTERVAL_MS } from '@/lib/syncPolling';

const VENDOR_COLORS = {
  cisco: 'var(--color-accent-info)',
  huawei: 'var(--color-accent-warning)',
  other: 'var(--color-text-muted)',
} as const;

/** Deep-links to /jobs with filters pre-applied for the given scope. Range is
 * fixed to `7d` so the linked view matches what the dashboard card summarises. */
export function jobsHrefForScope(scope: Scope, siteId: number | undefined): string {
  const params = new URLSearchParams({ range: '7d' });
  if (scope.kind === 'device') params.set('device', scope.deviceName);
  else if (siteId !== undefined) params.set('site_id', String(siteId));
  return `/jobs?${params.toString()}`;
}

export type Scope =
  | { kind: 'org' }
  | { kind: 'site'; siteId: number }
  | { kind: 'group'; groupId: number }
  | { kind: 'device'; deviceName: string };

/** Convierte el Scope del frontend en los query params que espera el endpoint. */
function scopeToSummaryParams(scope: Scope): DashboardSummaryParams {
  switch (scope.kind) {
    case 'org':
      return { scope: 'org' };
    case 'site':
      return { scope: 'site', id: scope.siteId };
    case 'group':
      return { scope: 'group', id: scope.groupId };
    case 'device':
      return { scope: 'device', name: scope.deviceName };
  }
}

interface Props {
  scope: Scope;
}

export function ScopeDashboard({ scope }: Props) {
  const queryClient = useQueryClient();

  // Una sola query trae todos los conteos del scope. Sustituye el patrón
  // anterior (1 getDevices + N getVlansSynced + N getPortsSynced + 1
  // getJobs = 3N+2 requests) por 1 sola. Polling automático cada 2s
  // mientras algún device tenga el lock Redis tomado, igual criterio
  // que usaba useQueries de vlans/ports antes de este refactor.
  const summaryQuery = useQuery<DashboardSummary>({
    queryKey: ['dashboard', 'summary', scope],
    queryFn: () => getDashboardSummary(scopeToSummaryParams(scope)),
    staleTime: 60_000,
    refetchInterval: (query) => {
      const data = query.state.data as DashboardSummary | undefined;
      return data && data.devices.sync_in_progress_count > 0 ? SYNC_POLL_INTERVAL_MS : false;
    },
  });

  const summary = summaryQuery.data;
  const loading = summaryQuery.isLoading;

  // VLAN name discrepancy: se calcula en el frontend a partir de
  // summary.vlans.entries[i].names.length > 1. Decisión de diseño para
  // no duplicar la lógica en backend.
  const vlanDerived = useMemo(() => {
    const entries = summary?.vlans.entries ?? [];
    const ids = entries.map((e) => e.id);
    const discrepancies = new Set<number>();
    const namesById = new Map<number, string[]>();
    for (const e of entries) {
      namesById.set(e.id, e.names);
      if (e.names.length > 1) discrepancies.add(e.id);
    }
    return { ids, discrepancies, namesById };
  }, [summary?.vlans.entries]);

  // Silent refresh reactivo on-open: al montar (o cambiar de scope), el
  // backend decide qué devices están stale (default 7 min) y encola sólo
  // esos, con coalescing. El botón manual se sacó -- el scheduler Celery
  // Beat (`sync_stale_devices_task`) mantiene el inventory fresco de
  // fondo cada ~20 min, y este disparador on-open cubre el caso "el user
  // vuelve al dashboard después de un rato". No mostramos loading global:
  // el spinner por-device del summary (via `sync_in_progress_count`) ya
  // señala qué se está refrescando.
  const scopeKey = JSON.stringify(scopeToSummaryParams(scope));
  const lastReactiveScope = useRef<string | null>(null);
  useEffect(() => {
    if (lastReactiveScope.current === scopeKey) return;
    lastReactiveScope.current = scopeKey;
    refreshDashboardScope(scopeToSummaryParams(scope))
      .then(() => {
        queryClient.invalidateQueries({ queryKey: ['dashboard', 'summary'] });
        queryClient.invalidateQueries({ queryKey: ['vlans', 'synced'] });
        queryClient.invalidateQueries({ queryKey: ['ports', 'synced'] });
      })
      .catch(() => {
        // Silent: refresh reactive es best-effort; el user ya está viendo
        // la data cacheada. Los errores reales del backend siguen
        // surfacing via `devices.sync_errors` en el summary.
      });
  }, [scopeKey, scope, queryClient]);

  const syncInProgress =
    (summary?.devices.sync_in_progress_count ?? 0) > 0;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-end">
        <LastSyncedLabel
          syncedAt={summary?.devices.last_sync_at ?? null}
          syncError={summary && summary.devices.sync_errors > 0 ? 'Sync error' : null}
          progressLabel={
            syncInProgress
              ? `${summary?.devices.sync_in_progress_count ?? 0} in progress`
              : null
          }
        />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <DevicesCard summary={summary} loading={loading} />
        <VlansCard
          summary={summary}
          loading={loading}
          vlanIds={vlanDerived.ids}
          discrepancies={vlanDerived.discrepancies}
          namesById={vlanDerived.namesById}
        />
        <PortsCard summary={summary} loading={loading} />
        <JobsCard
          summary={summary}
          loading={loading}
          jobsHref={jobsHrefForScope(scope, jobsSiteIdFor(scope))}
        />
      </div>
    </div>
  );
}

/** Enlace "View all →" al listado de jobs. Sigue queriendo un site_id
 * para pre-filtrar. Para scope=group el filtro por site queda a cargo
 * del listado de jobs (no crítico para el link inicial). */
function jobsSiteIdFor(scope: Scope): number | undefined {
  if (scope.kind === 'site') return scope.siteId;
  return undefined;
}

// ── Cards ────────────────────────────────────────────────────────────────────

interface CardProps {
  summary: DashboardSummary | undefined;
  loading: boolean;
}

function DevicesCard({ summary, loading }: CardProps) {
  // by_vendor viene con las keys que usa la DB (cisco_ios, huawei_vrp,
  // etc — el driver key, no el label). Agrupar por lo que muestra
  // vendorLabel() garantiza que cualquier vendor futuro con label
  // 'Cisco' o 'Huawei' caiga en el bucket correcto sin cambio.
  const byVendor = summary?.devices.by_vendor ?? {};
  let cisco = 0;
  let huawei = 0;
  let other = 0;
  for (const [key, count] of Object.entries(byVendor)) {
    const label = vendorLabel(key);
    if (label === 'Cisco') cisco += count;
    else if (label === 'Huawei') huawei += count;
    else other += count;
  }
  const total = summary?.devices.total ?? 0;

  return (
    <Panel title="Devices">
      <div className="flex items-center gap-4">
        <Pie
          size={100}
          strokeWidth={16}
          slices={[
            { value: cisco, color: VENDOR_COLORS.cisco },
            { value: huawei, color: VENDOR_COLORS.huawei },
            { value: other, color: VENDOR_COLORS.other },
          ]}
          center={
            <>
              <span className="text-lg font-semibold text-text leading-none">
                {loading ? '…' : total}
              </span>
              <span className="text-[10px] uppercase tracking-wide text-muted mt-1">
                Total
              </span>
            </>
          }
        />
        <div className="flex flex-col gap-1 text-sm">
          <VendorLegendRow
            color={VENDOR_COLORS.cisco}
            label="CISCO"
            value={cisco}
          />
          <VendorLegendRow
            color={VENDOR_COLORS.huawei}
            label="HUAWEI"
            value={huawei}
          />
          {other > 0 && (
            <VendorLegendRow
              color={VENDOR_COLORS.other}
              label="OTHER"
              value={other}
            />
          )}
        </div>
      </div>
    </Panel>
  );
}

function VendorLegendRow({
  color,
  label,
  value,
}: {
  color: string;
  label: string;
  value: number;
}) {
  return (
    <div className="flex items-center gap-2">
      <span
        className="inline-block h-3 w-3 rounded-sm"
        style={{ background: color }}
        aria-hidden
      />
      <span className="text-xs font-semibold uppercase tracking-wider text-muted w-16">
        {label}
      </span>
      <span className="text-sm font-semibold text-text tabular-nums">{value}</span>
    </div>
  );
}

interface VlansCardProps extends CardProps {
  vlanIds: number[];
  discrepancies: Set<number>;
  namesById: Map<number, string[]>;
}

function VlansCard({ summary, loading, vlanIds, discrepancies, namesById }: VlansCardProps) {
  return (
    <Panel title="VLANs">
      <BigNumber value={summary?.vlans.unique_count ?? 0} loading={loading} />
      <div className="mt-3 flex flex-wrap gap-1.5 max-h-40 overflow-y-auto">
        {vlanIds.map((id) => {
          const hasDiscrepancy = discrepancies.has(id);
          const names = namesById.get(id) ?? [];
          return (
            <span
              key={id}
              className="inline-flex items-center gap-0.5 rounded bg-panel-elev px-2 py-0.5 text-xs text-text tabular-nums"
              title={
                hasDiscrepancy
                  ? `VLAN ${id} — name differs across devices: ${names.join(', ')}`
                  : `VLAN ${id}${names[0] ? ` — ${names[0]}` : ''}`
              }
            >
              {id}
              {hasDiscrepancy && (
                <span className="text-warning font-bold leading-none" aria-hidden>
                  *
                </span>
              )}
            </span>
          );
        })}
        {vlanIds.length === 0 && !loading && (
          <span className="text-xs italic text-muted">No VLANs discovered</span>
        )}
      </div>
      {discrepancies.size > 0 && (
        <p className="text-[11px] text-muted mt-2">
          * marks VLAN IDs whose name differs between devices — hover to see the
          variants.
        </p>
      )}
    </Panel>
  );
}

function PortsCard({ summary, loading }: CardProps) {
  const ports = summary?.ports ?? { total: 0, up: 0, down: 0, shutdown: 0 };
  return (
    <Panel title="Ports">
      <BigNumber value={ports.total} loading={loading} />
      <table className="w-full mt-3 text-sm">
        <thead className="text-xs uppercase tracking-wider text-muted">
          <tr>
            <th className="text-left font-medium py-1">Status</th>
            <th className="text-right font-medium py-1">Quantity</th>
          </tr>
        </thead>
        <tbody className="tabular-nums">
          <tr className="border-t border-panel-border">
            <td className="py-1 font-semibold text-success">UP</td>
            <td className="py-1 text-right">{ports.up}</td>
          </tr>
          <tr className="border-t border-panel-border">
            <td className="py-1 font-semibold text-danger">DOWN</td>
            <td className="py-1 text-right">{ports.down}</td>
          </tr>
          <tr className="border-t border-panel-border">
            <td className="py-1 font-semibold text-muted">SHUTDOWN</td>
            <td className="py-1 text-right">{ports.shutdown}</td>
          </tr>
        </tbody>
      </table>
    </Panel>
  );
}

function JobsCard({
  summary,
  loading,
  jobsHref,
}: CardProps & { jobsHref: string }) {
  const jobsPie = useMemo<JobsPieData>(() => aggregateJobsPie(summary), [summary]);
  return (
    <Panel
      title="Jobs — last 7 days"
      actions={
        <Link
          href={jobsHref}
          className="text-xs uppercase tracking-wider text-info hover:brightness-125"
        >
          View all →
        </Link>
      }
    >
      {loading ? (
        <BigNumber value={0} loading />
      ) : (
        <JobsPieChart data={jobsPie} />
      )}
    </Panel>
  );
}

/** Traduce el ``by_status`` del backend a los 3 buckets que el
 * JobsPieChart entiende (success / failed / pending), más el conteo
 * separado de rollbacks. Mantiene los mismos criterios que el
 * ``computeTotals`` viejo. */
function aggregateJobsPie(summary: DashboardSummary | undefined): JobsPieData {
  if (!summary) return { success: 0, failed: 0, pending: 0, rollbackCount: 0 };
  const by = summary.jobs.by_status;
  const success = by.completed ?? 0;
  const failed = (by.failed ?? 0) + (by.cancelled ?? 0);
  const pending = (by.pending ?? 0) + (by.running ?? 0);
  return {
    success,
    failed,
    pending,
    rollbackCount: summary.jobs.rollback_performed_count,
  };
}

function BigNumber({ value, loading }: { value: number; loading: boolean }) {
  return (
    <div className="text-4xl font-bold tabular-nums text-text">
      {loading ? <span className="text-muted">…</span> : value.toLocaleString()}
    </div>
  );
}
