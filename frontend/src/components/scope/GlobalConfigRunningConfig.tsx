'use client';

import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  getGlobalConfigRunningConfigSynced,
  refreshDeviceGlobalConfig,
  type SyncedResource,
} from '@/services/api';
import type { GlobalConfigRunningConfigRead } from '@/types/global-config';
import { Panel } from './Panel';
import { RefreshButton } from './RefreshButton';
import { LineViewer } from './LineViewer';

interface Props {
  deviceName: string;
}

// Running-config sub-tab. Shares the `global_config` sync scope with the
// Overview / Routes / ACLs sub-tabs (one big scope, one refresh serves
// them all). The Refresh button here fires the same endpoint the other
// tabs would, so the user doesn't have to leave to update the dump.
export function GlobalConfigRunningConfig({ deviceName }: Props) {
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: ['running-config', 'synced', deviceName],
    queryFn: () => getGlobalConfigRunningConfigSynced(deviceName),
    enabled: Boolean(deviceName),
    refetchInterval: (q: {
      state: { data?: SyncedResource<GlobalConfigRunningConfigRead> };
    }) => (q.state.data?.sync_in_progress ? 2000 : false),
  });

  const inProgress = Boolean(query.data?.sync_in_progress);
  const syncedAt = query.data?.synced_at ?? null;
  const syncError = query.data?.sync_error ?? null;
  const lines = query.data?.data.running_config ?? null;

  const [filter, setFilter] = useState('');
  const [wrap, setWrap] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const t = setTimeout(() => setCopied(false), 1800);
    return () => clearTimeout(t);
  }, [copied]);

  async function handleRefresh() {
    setRefreshing(true);
    try {
      await refreshDeviceGlobalConfig(deviceName);
    } finally {
      // Refresh the general global_config scope invalidates the sibling
      // queries too, so switching sub-tabs shows the fresh data without
      // an extra roundtrip.
      queryClient.invalidateQueries({
        queryKey: ['running-config', 'synced', deviceName],
      });
      queryClient.invalidateQueries({
        queryKey: ['global-config', 'synced', deviceName],
      });
      queryClient.invalidateQueries({
        queryKey: ['global-config', 'version', 'synced', deviceName],
      });
      setRefreshing(false);
    }
  }

  async function handleCopy() {
    if (!lines) return;
    try {
      await navigator.clipboard.writeText(lines.join('\n'));
      setCopied(true);
    } catch {
      // Clipboard permission may be denied (older browsers, insecure
      // origins). Silent fail is fine -- the user can still select
      // the text manually in the viewer.
    }
  }

  const canCopy = lines !== null && lines.length > 0;

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
          <button
            type="button"
            onClick={handleCopy}
            disabled={!canCopy}
            className="rounded-md border border-panel-border bg-panel px-3 py-1.5 text-xs font-semibold uppercase tracking-wider text-text hover:bg-panel-elev disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            {copied ? 'Copied!' : 'Copy'}
          </button>
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
        title="Running-config"
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
          showLineNumbers
          neverSyncedLabel={
            'Running-config isn’t cached yet — click Refresh to pull it from the device. The dump can be big; the initial fetch may take a few seconds.'
          }
          emptyLabel="Running-config is empty."
        />
      </Panel>
    </div>
  );
}
