'use client';

import { useMemo, useState } from 'react';
import { useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  getDevices,
  getJobs,
  getPortsSynced,
  getVlansSynced,
  type SyncedResource,
} from '@/services/api';
import type { Device } from '@/types/device';
import type { Job } from '@/types/job';
import type { VlanEntry } from '@/types/vlan';
import type { PortListResponse } from '@/types/port';
import { vendorLabel } from '@/types/device';
import { Panel } from './Panel';
import { JobsPieChart, type JobsPieData } from './JobsPieChart';
import { Pie } from './Pie';
import { RefreshButton } from './RefreshButton';
import { runScopeRefresh } from './scopeRefresh';

const VENDOR_COLORS = {
  cisco: 'var(--color-accent-info)',
  huawei: 'var(--color-accent-warning)',
  other: 'var(--color-text-muted)',
} as const;

export type Scope =
  | { kind: 'org' }
  | { kind: 'site'; siteId: number }
  | { kind: 'group'; groupId: number };

/** Devices matching the scope. */
function useScopedDevices(scope: Scope) {
  return useQuery<Device[]>({
    queryKey: ['scope', 'devices', scope],
    queryFn: async () => {
      const all = await getDevices();
      switch (scope.kind) {
        case 'org':
          return all;
        case 'site':
          return all.filter((d) => d.site_id === scope.siteId);
        case 'group':
          return all.filter((d) => d.device_group_id === scope.groupId);
      }
    },
  });
}

interface Props {
  scope: Scope;
}

export function ScopeDashboard({ scope }: Props) {
  const queryClient = useQueryClient();
  const devicesQuery = useScopedDevices(scope);
  const devices = useMemo(() => devicesQuery.data ?? [], [devicesQuery.data]);
  const deviceNames = useMemo(() => devices.map((d) => d.name), [devices]);

  // Per-device VLAN + port envelopes. useQueries parallels across all devices;
  // React Query dedupes with the sidebar and any drill-down that reads the
  // same slice.
  // While the backend Celery task is running for a device, sync_in_progress
  // stays true — we poll every 2s until it flips to false so the "Last synced"
  // label updates on its own after the user clicks Refresh.
  const vlanQueries = useQueries({
    queries: deviceNames.map((name) => ({
      queryKey: ['vlans', 'synced', name],
      queryFn: () => getVlansSynced(name),
      enabled: Boolean(name),
      refetchInterval: (query: { state: { data?: SyncedResource<VlanEntry[]> } }) =>
        query.state.data?.sync_in_progress ? 2000 : false,
    })),
  });
  const portQueries = useQueries({
    queries: deviceNames.map((name) => ({
      queryKey: ['ports', 'synced', name],
      queryFn: () => getPortsSynced(name),
      enabled: Boolean(name),
      refetchInterval: (query: { state: { data?: SyncedResource<PortListResponse> } }) =>
        query.state.data?.sync_in_progress ? 2000 : false,
    })),
  });

  // Jobs — last 7 days. Backend only supports site_id (no group filter), so at
  // group scope we still ask by site and filter client-side by device name.
  const siteIdForJobs =
    scope.kind === 'site'
      ? scope.siteId
      : scope.kind === 'group'
      ? devices[0]?.site_id
      : undefined;
  const fromDate = useMemo(() => {
    const d = new Date();
    d.setDate(d.getDate() - 7);
    return d.toISOString();
  }, []);
  const jobsQuery = useQuery({
    queryKey: ['scope', 'jobs', scope, fromDate, siteIdForJobs],
    queryFn: () =>
      getJobs({
        site_id: siteIdForJobs,
        from_date: fromDate,
        page_size: 200,
      }),
    enabled: scope.kind === 'org' || siteIdForJobs !== undefined,
  });

  const totals = useMemo(
    () => computeTotals(devices, vlanQueries, portQueries, jobsQuery.data?.items ?? [], scope),
    [devices, vlanQueries, portQueries, jobsQuery.data, scope],
  );

  const syncMeta = useMemo(() => {
    // Oldest synced_at across VLANs + ports envelopes = the timestamp we
    // truthfully claim as "last synced" for the whole scope.
    const envelopes = [
      ...vlanQueries.map((q) => q.data as SyncedResource<VlanEntry[]> | undefined),
      ...portQueries.map((q) => q.data as SyncedResource<PortListResponse> | undefined),
    ].filter(Boolean) as SyncedResource<unknown>[];
    if (envelopes.length === 0) {
      return { syncedAt: null as string | null, error: null as string | null, inProgress: false };
    }
    let oldest: string | null = null;
    let error: string | null = null;
    let inProgress = false;
    for (const env of envelopes) {
      if (env.sync_error) error = env.sync_error;
      if (env.sync_in_progress) inProgress = true;
      if (env.synced_at && (oldest === null || env.synced_at < oldest)) oldest = env.synced_at;
    }
    return { syncedAt: oldest, error, inProgress };
  }, [vlanQueries, portQueries]);

  const [refreshState, setRefreshState] = useRefreshState();
  const loading =
    devicesQuery.isLoading ||
    vlanQueries.some((q) => q.isLoading) ||
    portQueries.some((q) => q.isLoading);

  async function handleRefresh() {
    setRefreshState({ running: true, done: 0, total: deviceNames.length });
    await runScopeRefresh(deviceNames, {
      onProgress: (done, total) => setRefreshState({ running: true, done, total }),
    });
    // Invalidate every synced envelope for these devices, plus jobs.
    for (const name of deviceNames) {
      queryClient.invalidateQueries({ queryKey: ['vlans', 'synced', name] });
      queryClient.invalidateQueries({ queryKey: ['ports', 'synced', name] });
    }
    queryClient.invalidateQueries({ queryKey: ['scope', 'jobs'] });
    setRefreshState({ running: false, done: 0, total: 0 });
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-end">
        <RefreshButton
          onClick={handleRefresh}
          loading={refreshState.running || syncMeta.inProgress}
          disabled={deviceNames.length === 0}
          syncedAt={syncMeta.syncedAt}
          syncError={syncMeta.error}
          progressLabel={
            refreshState.running
              ? `${refreshState.done} / ${refreshState.total} synced`
              : undefined
          }
        />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <DevicesCard totals={totals} loading={loading} />
        <VlansCard totals={totals} loading={loading} />
        <PortsCard totals={totals} loading={loading} />
        <JobsCard totals={totals} loading={jobsQuery.isLoading} />
      </div>
    </div>
  );
}

// ── Cards ────────────────────────────────────────────────────────────────────

interface CardProps {
  totals: ScopeTotals;
  loading: boolean;
}

function DevicesCard({ totals, loading }: CardProps) {
  return (
    <Panel title="Devices">
      <div className="flex items-center gap-4">
        <Pie
          size={100}
          strokeWidth={16}
          slices={[
            { value: totals.ciscoCount, color: VENDOR_COLORS.cisco },
            { value: totals.huaweiCount, color: VENDOR_COLORS.huawei },
            { value: totals.otherVendorCount, color: VENDOR_COLORS.other },
          ]}
          center={
            <>
              <span className="text-lg font-semibold text-text leading-none">
                {loading ? '…' : totals.deviceCount}
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
            value={totals.ciscoCount}
          />
          <VendorLegendRow
            color={VENDOR_COLORS.huawei}
            label="HUAWEI"
            value={totals.huaweiCount}
          />
          {totals.otherVendorCount > 0 && (
            <VendorLegendRow
              color={VENDOR_COLORS.other}
              label="OTHER"
              value={totals.otherVendorCount}
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

function VlansCard({ totals, loading }: CardProps) {
  return (
    <Panel title="VLANs">
      <BigNumber value={totals.vlanIds.length} loading={loading} />
      <div className="mt-3 flex flex-wrap gap-1.5 max-h-40 overflow-y-auto">
        {totals.vlanIds.map((id) => {
          const hasDiscrepancy = totals.vlanNameDiscrepancies.has(id);
          const names = totals.vlanNamesById.get(id) ?? [];
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
        {totals.vlanIds.length === 0 && (
          <span className="text-xs italic text-muted">No VLANs discovered</span>
        )}
      </div>
      {totals.vlanNameDiscrepancies.size > 0 && (
        <p className="text-[11px] text-muted mt-2">
          * marks VLAN IDs whose name differs between devices — hover to see the
          variants.
        </p>
      )}
    </Panel>
  );
}

function PortsCard({ totals, loading }: CardProps) {
  return (
    <Panel title="Ports">
      <BigNumber value={totals.portTotal} loading={loading} />
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
            <td className="py-1 text-right">{totals.portUp}</td>
          </tr>
          <tr className="border-t border-panel-border">
            <td className="py-1 font-semibold text-danger">DOWN</td>
            <td className="py-1 text-right">{totals.portDown}</td>
          </tr>
          <tr className="border-t border-panel-border">
            <td className="py-1 font-semibold text-muted">SHUTDOWN</td>
            <td className="py-1 text-right">{totals.portShutdown}</td>
          </tr>
        </tbody>
      </table>
    </Panel>
  );
}

function JobsCard({ totals, loading }: CardProps) {
  return (
    <Panel title="Jobs — last 7 days">
      {loading ? (
        <BigNumber value={0} loading />
      ) : (
        <JobsPieChart data={totals.jobs} />
      )}
    </Panel>
  );
}

function BigNumber({ value, loading }: { value: number; loading: boolean }) {
  return (
    <div className="text-4xl font-bold tabular-nums text-text">
      {loading ? <span className="text-muted">…</span> : value.toLocaleString()}
    </div>
  );
}

// ── Aggregation ──────────────────────────────────────────────────────────────

interface ScopeTotals {
  deviceCount: number;
  ciscoCount: number;
  huaweiCount: number;
  otherVendorCount: number;
  vlanIds: number[];
  vlanNamesById: Map<number, string[]>;
  vlanNameDiscrepancies: Set<number>;
  portTotal: number;
  portUp: number;
  portDown: number;
  portShutdown: number;
  jobs: JobsPieData;
}

function computeTotals(
  devices: Device[],
  vlanQueries: ReturnType<typeof useQueries> extends infer T ? T : never,
  portQueries: ReturnType<typeof useQueries> extends infer T ? T : never,
  jobs: Job[],
  scope: Scope,
): ScopeTotals {
  // ── Devices by vendor ─────────────────────────────────────────────────────
  let ciscoCount = 0;
  let huaweiCount = 0;
  let otherVendorCount = 0;
  for (const d of devices) {
    const label = vendorLabel(d.vendor);
    if (label === 'Cisco') ciscoCount += 1;
    else if (label === 'Huawei') huaweiCount += 1;
    else otherVendorCount += 1;
  }

  // ── VLAN OR-union with per-id name tracking ───────────────────────────────
  const vlanNames = new Map<number, Set<string>>();
  const vqs = vlanQueries as { data?: SyncedResource<VlanEntry[]> }[];
  for (const q of vqs) {
    if (!q?.data) continue;
    for (const entry of q.data.data) {
      const bucket = vlanNames.get(entry.vlan_id) ?? new Set<string>();
      if (entry.name) bucket.add(entry.name);
      vlanNames.set(entry.vlan_id, bucket);
    }
  }
  const vlanIds = Array.from(vlanNames.keys()).sort((a, b) => a - b);
  const vlanNamesById = new Map<number, string[]>();
  const vlanNameDiscrepancies = new Set<number>();
  for (const [id, names] of vlanNames) {
    const arr = Array.from(names);
    vlanNamesById.set(id, arr);
    if (arr.length > 1) vlanNameDiscrepancies.add(id);
  }

  // ── Port state buckets ────────────────────────────────────────────────────
  let portTotal = 0;
  let portUp = 0;
  let portDown = 0;
  let portShutdown = 0;
  const pqs = portQueries as { data?: SyncedResource<PortListResponse> }[];
  for (const q of pqs) {
    if (!q?.data) continue;
    const ports = q.data.data.ports;
    portTotal += ports.length;
    for (const p of ports) {
      if (p.admin_up === false) portShutdown += 1;
      else if (p.operational_up === true) portUp += 1;
      else if (p.operational_up === false) portDown += 1;
      // Unknown state (nulls) contribute to total but not to any bucket.
    }
  }

  // ── Jobs — apply group-scope client-side filter if needed ────────────────
  const deviceNameSet = new Set(devices.map((d) => d.name));
  const scopedJobs =
    scope.kind === 'group'
      ? jobs.filter((j) => j.device !== null && deviceNameSet.has(j.device))
      : jobs;

  const jobsAgg: JobsPieData = {
    success: 0,
    failed: 0,
    pending: 0,
    rollbackCount: 0,
  };
  for (const j of scopedJobs) {
    if (j.status === 'completed') jobsAgg.success += 1;
    else if (
      j.status === 'failed' ||
      j.status === 'cancelled' ||
      j.status === 'rollback_performed'
    )
      jobsAgg.failed += 1;
    else jobsAgg.pending += 1;
    if (j.rollback_performed) jobsAgg.rollbackCount += 1;
  }

  return {
    deviceCount: devices.length,
    ciscoCount,
    huaweiCount,
    otherVendorCount,
    vlanIds,
    vlanNamesById,
    vlanNameDiscrepancies,
    portTotal,
    portUp,
    portDown,
    portShutdown,
    jobs: jobsAgg,
  };
}

// ── Refresh state (tiny local hook to avoid a full context) ──────────────────

interface RefreshState {
  running: boolean;
  done: number;
  total: number;
}

function useRefreshState(): [RefreshState, (s: RefreshState) => void] {
  return useState<RefreshState>({ running: false, done: 0, total: 0 });
}
