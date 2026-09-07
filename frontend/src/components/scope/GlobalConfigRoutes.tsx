'use client';

import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  getGlobalConfigSynced,
  refreshDeviceGlobalConfig,
  removeGlobalConfigRoute,
  type SyncedResource,
} from '@/services/api';
import type {
  GlobalConfigRead,
  GlobalConfigRouteEntry,
} from '@/types/global-config';
import { useJobNotifications } from '@/context/JobNotificationContext';
import { Panel } from './Panel';
import { RefreshButton } from './RefreshButton';
import { SYNC_POLL_INTERVAL_MS } from '@/lib/syncPolling';
import { RouteAddModal } from './RouteAddModal';
import { extractMessage } from './VlanCreateModal';

interface Props {
  deviceName: string;
}

// Routes section — shares the same `global_config` sync scope as the
// Overview (so we reuse the same query key: 1 refresh, 1 cache). Rows are
// read-only from the parser's shape (destination / next_hop / interface,
// each individually nullable). Delete is enabled only for rows where BOTH
// destination and next_hop are known -- the backend endpoint takes both
// values as the identity of the route to remove.
export function GlobalConfigRoutes({ deviceName }: Props) {
  const queryClient = useQueryClient();
  const { trackJob, trackGroupJob } = useJobNotifications();

  const configQuery = useQuery({
    queryKey: ['global-config', 'synced', deviceName],
    queryFn: () => getGlobalConfigSynced(deviceName),
    enabled: Boolean(deviceName),
    refetchInterval: (query: {
      state: { data?: SyncedResource<GlobalConfigRead> };
    }) => (query.state.data?.sync_in_progress ? SYNC_POLL_INTERVAL_MS : false),
  });

  const inProgress = Boolean(configQuery.data?.sync_in_progress);
  const syncedAt = configQuery.data?.synced_at ?? null;
  const syncError = configQuery.data?.sync_error ?? null;
  const routes = configQuery.data?.data.routes ?? null;

  const [refreshing, setRefreshing] = useState(false);
  const [openAdd, setOpenAdd] = useState(false);
  const [toast, setToast] = useState<{ msg: string; tone: 'ok' | 'error' } | null>(
    null,
  );
  const [deletingKey, setDeletingKey] = useState<string | null>(null);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 4500);
    return () => clearTimeout(t);
  }, [toast]);

  async function handleRefresh() {
    setRefreshing(true);
    try {
      await refreshDeviceGlobalConfig(deviceName);
    } finally {
      queryClient.invalidateQueries({
        queryKey: ['global-config', 'synced', deviceName],
      });
      queryClient.invalidateQueries({
        queryKey: ['global-config', 'version', 'synced', deviceName],
      });
      setRefreshing(false);
    }
  }

  async function handleDelete(destination: string, next_hop: string) {
    const key = `${destination}|${next_hop}`;
    setDeletingKey(key);
    try {
      const result = await removeGlobalConfigRoute(deviceName, {
        destination,
        next_hop,
      });
      const label = `Remove route ${destination} → ${next_hop} on ${deviceName}`;
      trackGroupJob(result.group_job_id, label);
      for (const j of result.jobs) {
        trackJob(j.job_id, label, j.device);
      }
      queryClient.invalidateQueries({
        queryKey: ['global-config', 'synced', deviceName],
      });
      setToast({
        msg: `Remove route ${destination} queued on ${deviceName}.`,
        tone: 'ok',
      });
    } catch (err) {
      setToast({
        msg: extractMessage(err, 'Remove route failed.'),
        tone: 'error',
      });
    } finally {
      setDeletingKey(null);
    }
  }

  const loading = configQuery.isLoading;
  const neverSynced =
    !loading && !inProgress && !syncError && syncedAt === null && routes === null;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between gap-2">
        <button
          type="button"
          onClick={() => setOpenAdd(true)}
          disabled={deletingKey !== null}
          className="rounded-md px-4 py-2 text-sm font-semibold uppercase tracking-wider border border-panel-border text-text hover:bg-panel-elev disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
        >
          Add route
        </button>
        <RefreshButton
          onClick={handleRefresh}
          loading={refreshing || inProgress}
          syncedAt={syncedAt}
          syncError={syncError}
        />
      </div>

      {syncError && (
        <div className="rounded-md border border-danger/60 bg-danger/10 text-danger px-3 py-2 text-sm">
          Last sync failed: {syncError}
        </div>
      )}

      {neverSynced && (
        <div className="rounded-md border border-panel-border bg-panel-elev/40 text-muted px-3 py-2 text-sm italic">
          No global-config data cached yet — click Refresh to pull it from the device.
        </div>
      )}

      <Panel title="Routes">
        <RoutesTable
          routes={routes}
          onDelete={handleDelete}
          deletingKey={deletingKey}
          loading={loading}
        />
      </Panel>

      <RouteAddModal
        open={openAdd}
        onClose={() => setOpenAdd(false)}
        deviceName={deviceName}
        onDone={(msg, tone) => setToast({ msg, tone })}
      />

      {toast && (
        <div
          className={`fixed bottom-4 right-4 z-40 rounded-md px-4 py-2 shadow-lg text-sm ${
            toast.tone === 'ok' ? 'bg-success text-white' : 'bg-danger text-white'
          }`}
        >
          {toast.msg}
        </div>
      )}
    </div>
  );
}

function RoutesTable({
  routes,
  onDelete,
  deletingKey,
  loading,
}: {
  routes: GlobalConfigRouteEntry[] | null;
  onDelete: (destination: string, next_hop: string) => void;
  deletingKey: string | null;
  loading: boolean;
}) {
  if (loading) {
    return <p className="text-sm italic text-muted">Loading…</p>;
  }
  if (routes === null) {
    return (
      <p className="text-sm italic text-muted">
        No route data cached yet.
      </p>
    );
  }
  if (routes.length === 0) {
    return (
      <p className="text-sm italic text-muted">
        No static routes configured.
      </p>
    );
  }
  return (
    <table className="w-full text-sm">
      <thead className="text-xs uppercase tracking-wider text-muted">
        <tr>
          <th className="text-left font-medium py-1">Destination</th>
          <th className="text-left font-medium py-1">Next-hop</th>
          <th className="text-left font-medium py-1">Interface</th>
          <th className="text-right font-medium py-1 w-16">Actions</th>
        </tr>
      </thead>
      <tbody className="font-mono">
        {routes.map((r, idx) => {
          const destination =
            typeof r.destination === 'string' ? r.destination : null;
          const next_hop = typeof r.next_hop === 'string' ? r.next_hop : null;
          const iface = typeof r.interface === 'string' ? r.interface : null;
          const key = `${destination ?? '?'}|${next_hop ?? '?'}|${idx}`;
          const canDelete = destination !== null && next_hop !== null;
          const rowKey = `${destination}|${next_hop}`;
          const isDeleting = deletingKey === rowKey;
          return (
            <tr key={key} className="border-t border-panel-border">
              <td className="py-1 text-text">{destination ?? '—'}</td>
              <td className="py-1 text-text">{next_hop ?? '—'}</td>
              <td className="py-1 text-text">{iface ?? '—'}</td>
              <td className="py-1 text-right">
                {canDelete ? (
                  <button
                    type="button"
                    onClick={() => onDelete(destination, next_hop)}
                    disabled={deletingKey !== null}
                    aria-label={`Remove ${destination} → ${next_hop}`}
                    className="text-danger hover:brightness-125 disabled:opacity-40 disabled:cursor-not-allowed text-lg leading-none"
                  >
                    {isDeleting ? '…' : '×'}
                  </button>
                ) : (
                  <span
                    className="text-muted"
                    title="Delete requires both destination and next-hop"
                  >
                    —
                  </span>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
