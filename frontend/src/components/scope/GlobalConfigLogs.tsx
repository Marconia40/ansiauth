'use client';

import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  getDeviceLogsSynced,
  refreshDeviceLogs,
  type SyncedResource,
} from '@/services/api';
import type { DeviceLogsRead } from '@/types/global-config';
import { Panel } from './Panel';
import { RefreshButton } from './RefreshButton';
import { LineViewer } from './LineViewer';

interface Props {
  deviceName: string;
}

// Logs sub-tab. Own sync scope (`logs`), same criterion as ARP/MAC -- not
// populated on device registration nor pulled by the general Refresh, has
// to be requested explicitly. On Cisco, config-audit lines
// (%PARSER-5-CFGLOG_LOGGEDCMD, which echo back the full text of every
// applied config command) are excluded at the source per the backend, so
// what shows up here is real operational events only.
export function GlobalConfigLogs({ deviceName }: Props) {
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: ['device-logs', 'synced', deviceName],
    queryFn: () => getDeviceLogsSynced(deviceName),
    enabled: Boolean(deviceName),
    refetchInterval: (q: {
      state: { data?: SyncedResource<DeviceLogsRead> };
    }) => (q.state.data?.sync_in_progress ? 2000 : false),
  });

  const inProgress = Boolean(query.data?.sync_in_progress);
  const syncedAt = query.data?.synced_at ?? null;
  const syncError = query.data?.sync_error ?? null;
  const lines = query.data?.data.log_lines ?? null;

  const [filter, setFilter] = useState('');
  const [wrap, setWrap] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  async function handleRefresh() {
    setRefreshing(true);
    try {
      await refreshDeviceLogs(deviceName);
    } finally {
      queryClient.invalidateQueries({
        queryKey: ['device-logs', 'synced', deviceName],
      });
      setRefreshing(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-3 flex-1 min-w-0">
          <input
            type="text"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter (case-insensitive substring)"
            className="flex-1 max-w-md rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
          />
          <label className="flex items-center gap-2 text-sm text-muted">
            <input
              type="checkbox"
              checked={wrap}
              onChange={(e) => setWrap(e.target.checked)}
              className="accent-info"
            />
            Wrap
          </label>
        </div>
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

      <Panel
        title="Device logs"
        actions={
          <span className="text-xs text-muted tabular-nums">
            {lines?.length ?? 0} line{(lines?.length ?? 0) === 1 ? '' : 's'}
          </span>
        }
      >
        <LineViewer
          lines={lines}
          filter={filter}
          wrap={wrap}
          neverSyncedLabel={
            'Local log buffer isn’t synced automatically on device registration — click Refresh to pull it. Big buffers can take a while.'
          }
          emptyLabel="Log buffer is empty."
        />
      </Panel>
    </div>
  );
}
