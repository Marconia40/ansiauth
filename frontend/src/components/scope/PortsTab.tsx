'use client';

import { useMemo, useState } from 'react';
import { useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import Link from 'next/link';
import { getDevices, getPortsSynced, type SyncedResource } from '@/services/api';
import { useJobNotifications } from '@/context/JobNotificationContext';
import type { Device } from '@/types/device';
import type { Port, PortListResponse } from '@/types/port';
import type { Scope } from './ScopeDashboard';
import { Panel } from './Panel';
import { RefreshButton } from './RefreshButton';
import { runScopeRefresh } from './scopeRefresh';
import { ChassisMulti } from './ChassisMulti';
import { PortSelectionSummary } from './PortSelectionSummary';
import { PortActionRail, type PortActionId } from './PortActionRail';
import { usePortSelection } from './usePortSelection';
import type { BatchResult } from './runPortBatch';
import { PortResetModal } from './PortActionModals';
import { PortEditModal } from './PortEditModal';
import { SYNC_POLL_INTERVAL_MS } from '@/lib/syncPolling';

interface Props {
  scope: Scope;
}

export function PortsTab({ scope }: Props) {
  const queryClient = useQueryClient();
  const { trackGroupJob } = useJobNotifications();
  const selection = usePortSelection();

  const devicesQuery = useQuery<Device[]>({
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
        case 'device':
          return all.filter((d) => d.name === scope.deviceName);
      }
    },
  });
  const devices = useMemo(() => devicesQuery.data ?? [], [devicesQuery.data]);
  const deviceNames = useMemo(() => devices.map((d) => d.name), [devices]);

  const portQueries = useQueries({
    queries: deviceNames.map((name) => ({
      queryKey: ['ports', 'synced', name],
      queryFn: () => getPortsSynced(name),
      enabled: Boolean(name),
      refetchInterval: (query: { state: { data?: SyncedResource<PortListResponse> } }) =>
        query.state.data?.sync_in_progress ? SYNC_POLL_INTERVAL_MS : false,
    })),
  });

  const portsByDevice = useMemo(() => {
    const map = new Map<string, Port[]>();
    deviceNames.forEach((name, idx) => {
      const envelope = portQueries[idx]?.data as
        | SyncedResource<PortListResponse>
        | undefined;
      if (envelope) map.set(name, envelope.data.ports);
    });
    return map;
  }, [deviceNames, portQueries]);

  const syncMeta = useMemo(() => {
    const envelopes = portQueries
      .map((q) => q.data as SyncedResource<PortListResponse> | undefined)
      .filter(Boolean) as SyncedResource<unknown>[];
    let oldest: string | null = null;
    let error: string | null = null;
    let inProgress = false;
    for (const env of envelopes) {
      if (env.sync_error) error = env.sync_error;
      if (env.sync_in_progress) inProgress = true;
      if (env.synced_at && (oldest === null || env.synced_at < oldest)) oldest = env.synced_at;
    }
    return { syncedAt: oldest, error, inProgress };
  }, [portQueries]);

  const [refreshing, setRefreshing] = useState(false);
  async function handleRefresh() {
    setRefreshing(true);
    await runScopeRefresh(deviceNames);
    for (const n of deviceNames) {
      queryClient.invalidateQueries({ queryKey: ['ports', 'synced', n] });
    }
    setRefreshing(false);
  }

  const [openAction, setOpenAction] = useState<PortActionId | null>(null);

  // Every `reset` succeeds as its own group_job on the backend — register
  // each so the global JobNotifications toast is clickable → JobDetailModal
  // (same UX as Routes / SVI). `PortEditModal` handles its own tracking
  // internally, so `handleDone` here only sees `perPortJobs` from the reset
  // path; `groupJobsByDevice` (batch) is a no-op no-such-key iteration.
  function handleDone(result: BatchResult) {
    for (const { ref, groupJobId } of result.perPortJobs ?? []) {
      trackGroupJob(groupJobId, `Reset ${ref.interface} on ${ref.device}`);
    }
  }

  if (devicesQuery.isLoading) {
    return <p className="text-sm italic text-muted py-6">Loading devices…</p>;
  }
  if (devices.length === 0) {
    return <p className="text-sm italic text-muted py-6">No devices in scope.</p>;
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

      <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_16rem] gap-4">
        {/* ── Left column: Devices + Selection summary ─────────────────── */}
        <div className="flex flex-col gap-4 min-w-0">
          <Panel title="Devices" className="max-h-[60vh] overflow-y-auto">
            <div className="flex flex-col gap-6">
              {devices.map((device, idx) => {
                const envelope = portQueries[idx]?.data as
                  | SyncedResource<PortListResponse>
                  | undefined;
                const ports = envelope?.data.ports ?? [];
                const isLoading = portQueries[idx]?.isLoading;
                const deviceSelectedCount =
                  selection.byDevice.get(device.name)?.length ?? 0;
                return (
                  <div key={device.name} className="flex flex-col gap-2">
                    <div className="flex items-baseline justify-between gap-3">
                      <Link
                        href={`/devices/${encodeURIComponent(device.name)}`}
                        className="text-base font-semibold text-text hover:text-info transition-colors"
                      >
                        {device.name}
                      </Link>
                      <span className="text-xs text-muted tabular-nums">
                        {ports.length} port{ports.length === 1 ? '' : 's'}
                        {deviceSelectedCount > 0 && (
                          <>
                            {' · '}
                            <span className="text-info font-semibold">
                              {deviceSelectedCount} selected
                            </span>
                          </>
                        )}
                      </span>
                    </div>
                    {isLoading ? (
                      <p className="text-xs italic text-muted">Loading ports…</p>
                    ) : ports.length === 0 ? (
                      <p className="text-xs italic text-muted">
                        No ports collected — refresh to sync.
                      </p>
                    ) : (
                      <div className="overflow-x-auto">
                        <ChassisMulti
                          device={device.name}
                          ports={ports}
                          selection={selection}
                        />
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </Panel>

          <Panel title="Selection" className="max-h-[50vh] overflow-y-auto">
            <PortSelectionSummary
              selection={selection}
              portsByDevice={portsByDevice}
            />
          </Panel>
        </div>

        {/* ── Right column: action rail ───────────────────────────────── */}
        <aside>
          <PortActionRail
            disabled={selection.count === 0}
            onOpen={(id) => setOpenAction(id)}
          />
        </aside>
      </div>

      {/* ── Modals ───────────────────────────────────────────────────── */}
      <PortEditModal
        open={openAction === 'edit'}
        onClose={() => setOpenAction(null)}
        selection={selection}
      />
      <PortResetModal
        open={openAction === 'reset'}
        onClose={() => setOpenAction(null)}
        selection={selection}
        onDone={handleDone}
      />
    </div>
  );
}
