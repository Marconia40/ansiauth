'use client';

import { useMemo, useState } from 'react';
import Link from 'next/link';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  getDevice,
  getJobs,
  getPortsSynced,
  type SyncedResource,
} from '@/services/api';
import type { Port, PortListResponse } from '@/types/port';
import type { Job } from '@/types/job';
import { Panel } from './Panel';
import { RefreshButton } from './RefreshButton';
import { runScopeRefresh } from './scopeRefresh';
import { ChassisGrid, classifyPort } from './ChassisGrid';
import { PortDetailCard } from './PortDetailCard';
import { JobsPieChart, type JobsPieData } from './JobsPieChart';
import { jobsHrefForScope } from './ScopeDashboard';

interface Props {
  deviceName: string;
}

export function DeviceDashboard({ deviceName }: Props) {
  const queryClient = useQueryClient();

  const deviceQuery = useQuery({
    queryKey: ['device', deviceName],
    queryFn: () => getDevice(deviceName),
    enabled: Boolean(deviceName),
  });

  // Ports come with the SyncedResource envelope so we can render "Last synced"
  // and auto-poll while a Celery refresh is in flight — same pattern as ScopeDashboard.
  const portsQuery = useQuery({
    queryKey: ['ports', 'synced', deviceName],
    queryFn: () => getPortsSynced(deviceName),
    enabled: Boolean(deviceName),
    refetchInterval: (query: { state: { data?: SyncedResource<PortListResponse> } }) =>
      query.state.data?.sync_in_progress ? 2000 : false,
  });

  // Jobs — last 7 days. Backend filter accepts site_id but not device name in
  // a reliable way, so we scope by the device's site and filter client-side.
  const siteId = deviceQuery.data?.site_id;
  const fromDate = useMemo(() => {
    const d = new Date();
    d.setDate(d.getDate() - 7);
    return d.toISOString();
  }, []);
  const jobsQuery = useQuery({
    queryKey: ['device', deviceName, 'jobs', fromDate, siteId],
    queryFn: () => getJobs({ site_id: siteId, from_date: fromDate, page_size: 200 }),
    enabled: Boolean(deviceName) && siteId !== undefined,
  });

  const ports = useMemo(
    () => portsQuery.data?.data.ports ?? [],
    [portsQuery.data],
  );
  const portTotal = portsQuery.data?.data.count ?? ports.length;

  const [selectedPortName, setSelectedPortName] = useState<string | null>(null);
  const selectedPort =
    ports.find((p) => p.name === selectedPortName) ?? null;

  const portsSummary = useMemo(() => tallyPorts(ports), [ports]);
  const jobsData = useMemo<JobsPieData>(
    () => tallyJobs(jobsQuery.data?.items ?? [], deviceName),
    [jobsQuery.data, deviceName],
  );

  const syncMeta = portsQuery.data
    ? {
        syncedAt: portsQuery.data.synced_at,
        error: portsQuery.data.sync_error,
        inProgress: portsQuery.data.sync_in_progress,
      }
    : { syncedAt: null, error: null, inProgress: false };

  const [refreshing, setRefreshing] = useState(false);
  async function handleRefresh() {
    setRefreshing(true);
    await runScopeRefresh([deviceName]);
    queryClient.invalidateQueries({ queryKey: ['vlans', 'synced', deviceName] });
    queryClient.invalidateQueries({ queryKey: ['ports', 'synced', deviceName] });
    queryClient.invalidateQueries({ queryKey: ['device', deviceName, 'jobs'] });
    setRefreshing(false);
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-end">
        <RefreshButton
          onClick={handleRefresh}
          loading={refreshing || syncMeta.inProgress}
          syncedAt={syncMeta.syncedAt}
          syncError={syncMeta.error}
        />
      </div>

      {/* Chassis + selected port ─────────────────────────────────────────── */}
      <Panel title={deviceQuery.data?.name ?? deviceName}>
        {portsQuery.isLoading ? (
          <ChassisSkeleton />
        ) : ports.length === 0 ? (
          <p className="text-sm italic text-muted py-4">
            No ports collected yet — try clicking Refresh.
          </p>
        ) : (
          <div className="flex flex-col gap-4">
            <div className="overflow-x-auto">
              <ChassisGrid
                ports={ports}
                selectedName={selectedPortName}
                onSelect={setSelectedPortName}
              />
            </div>
            <PortDetailCard port={selectedPort} />
          </div>
        )}
      </Panel>

      {/* Ports summary + Jobs pie ─────────────────────────────────────────── */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Panel title="Ports">
          <div className="text-sm text-muted mb-2">
            Total = <span className="text-text font-semibold">{portTotal}</span>
          </div>
          <table className="w-full text-sm">
            <thead className="text-xs uppercase tracking-wider text-muted">
              <tr>
                <th className="text-left font-medium py-1">Status</th>
                <th className="text-right font-medium py-1">Quantity</th>
              </tr>
            </thead>
            <tbody className="tabular-nums">
              <tr className="border-t border-panel-border">
                <td className="py-1 font-semibold text-success">UP</td>
                <td className="py-1 text-right">{portsSummary.up}</td>
              </tr>
              <tr className="border-t border-panel-border">
                <td className="py-1 font-semibold text-danger">DOWN</td>
                <td className="py-1 text-right">{portsSummary.down}</td>
              </tr>
              <tr className="border-t border-panel-border">
                <td className="py-1 font-semibold text-muted">SHUTDOWN</td>
                <td className="py-1 text-right">{portsSummary.shutdown}</td>
              </tr>
            </tbody>
          </table>
        </Panel>

        <Panel
          title="Jobs — last 7 days"
          actions={
            <Link
              href={jobsHrefForScope({ kind: 'device', deviceName }, siteId)}
              className="text-xs uppercase tracking-wider text-info hover:brightness-125"
            >
              View all →
            </Link>
          }
        >
          {jobsQuery.isLoading ? (
            <div className="text-sm text-muted">…</div>
          ) : (
            <JobsPieChart data={jobsData} />
          )}
        </Panel>
      </div>
    </div>
  );
}

// ── Aggregation helpers ──────────────────────────────────────────────────────

function tallyPorts(ports: Port[]): { up: number; down: number; shutdown: number } {
  let up = 0;
  let down = 0;
  let shutdown = 0;
  for (const p of ports) {
    const state = classifyPort(p);
    if (state === 'up') up += 1;
    else if (state === 'down') down += 1;
    else if (state === 'shutdown') shutdown += 1;
  }
  return { up, down, shutdown };
}

function tallyJobs(jobs: Job[], deviceName: string): JobsPieData {
  const agg: JobsPieData = { success: 0, failed: 0, pending: 0, rollbackCount: 0 };
  for (const j of jobs) {
    if (j.device !== deviceName) continue;
    if (j.status === 'completed') agg.success += 1;
    else if (
      j.status === 'failed' ||
      j.status === 'cancelled' ||
      j.status === 'rollback_performed'
    )
      agg.failed += 1;
    else agg.pending += 1;
    if (j.rollback_performed) agg.rollbackCount += 1;
  }
  return agg;
}

function ChassisSkeleton() {
  return (
    <div
      className="grid gap-1.5 p-3 rounded-md bg-panel-elev/40 border border-panel-border animate-pulse"
      style={{ gridTemplateColumns: 'repeat(24, minmax(0, 1fr))' }}
    >
      {Array.from({ length: 48 }).map((_, i) => (
        <div key={i} className="h-6 w-6 rounded-sm bg-panel-border" />
      ))}
    </div>
  );
}
