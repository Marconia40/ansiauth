'use client';

import { useEffect, useMemo, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  getArpTable,
  getMacTable,
  refreshDeviceArpMac,
  type SyncedResource,
} from '@/services/api';
import type {
  ArpMacEntry,
  ArpTableRead,
  MacTableRead,
} from '@/types/global-config';
import { Panel } from './Panel';
import { RefreshButton } from './RefreshButton';
import { SYNC_POLL_INTERVAL_MS } from '@/lib/syncPolling';

interface Props {
  deviceName: string;
}

// Mirrors backend/app/api/global_config.py:_ARP_MAC_INCLUDE_RE. Kept here
// too so the input can flag invalid characters before the request goes
// out (backend would 422 otherwise). Same criteria on both sides means
// no user is ever surprised by "your filter isn't allowed" only after
// hitting Enter.
const INCLUDE_RE = /^[A-Za-z0-9:./_-]{1,64}$/;

const ARP_COLUMNS = ['ip', 'mac', 'age', 'type', 'interface', 'vlan'] as const;
const MAC_COLUMNS = ['mac', 'vlan', 'type', 'interface'] as const;

// ARP/MAC live in their OWN sync scope (`arp_mac`), separate from the
// general `global_config`. Not populated on device registration and not
// pulled by the general Refresh -- the user has to hit "Refresh ARP/MAC"
// explicitly. The 2 queries share the sync scope so `synced_at` /
// `sync_error` / `sync_in_progress` should match between them.
export function GlobalConfigArpMac({ deviceName }: Props) {
  const queryClient = useQueryClient();

  // Raw input state + debounced value that actually drives the queries.
  // Debounce keeps us from firing 2 requests on every keystroke; 300ms
  // is the standard "search-as-you-type" delay.
  const [rawInclude, setRawInclude] = useState('');
  const [debouncedInclude, setDebouncedInclude] = useState('');
  useEffect(() => {
    const t = setTimeout(() => setDebouncedInclude(rawInclude), 300);
    return () => clearTimeout(t);
  }, [rawInclude]);

  const includeValid = rawInclude === '' || INCLUDE_RE.test(rawInclude);
  const includeParam =
    debouncedInclude !== '' && INCLUDE_RE.test(debouncedInclude)
      ? debouncedInclude
      : undefined;

  const arpQuery = useQuery({
    queryKey: ['arp-table', deviceName, includeParam ?? ''],
    queryFn: () => getArpTable(deviceName, includeParam),
    enabled: Boolean(deviceName),
    refetchInterval: (query: {
      state: { data?: SyncedResource<ArpTableRead> };
    }) => (query.state.data?.sync_in_progress ? SYNC_POLL_INTERVAL_MS : false),
  });

  const macQuery = useQuery({
    queryKey: ['mac-table', deviceName, includeParam ?? ''],
    queryFn: () => getMacTable(deviceName, includeParam),
    enabled: Boolean(deviceName),
    refetchInterval: (query: {
      state: { data?: SyncedResource<MacTableRead> };
    }) => (query.state.data?.sync_in_progress ? SYNC_POLL_INTERVAL_MS : false),
  });

  const inProgress = Boolean(
    arpQuery.data?.sync_in_progress || macQuery.data?.sync_in_progress,
  );
  const syncedAt =
    arpQuery.data?.synced_at ?? macQuery.data?.synced_at ?? null;
  const syncError =
    arpQuery.data?.sync_error ?? macQuery.data?.sync_error ?? null;

  const arpEntries = arpQuery.data?.data.entries ?? null;
  const macEntries = macQuery.data?.data.entries ?? null;

  const [refreshing, setRefreshing] = useState(false);
  async function handleRefresh() {
    setRefreshing(true);
    try {
      await refreshDeviceArpMac(deviceName);
    } finally {
      queryClient.invalidateQueries({ queryKey: ['arp-table', deviceName] });
      queryClient.invalidateQueries({ queryKey: ['mac-table', deviceName] });
      setRefreshing(false);
    }
  }

  const loading = arpQuery.isLoading || macQuery.isLoading;
  // "Never synced" — the cache is empty (both entries null AND no
  // synced_at). If the user filters and the include yields no rows, we
  // want a different empty state, hence the null vs [] distinction.
  const neverSynced =
    !loading &&
    !inProgress &&
    !syncError &&
    syncedAt === null &&
    arpEntries === null &&
    macEntries === null;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between gap-2">
        <div className="flex-1 max-w-md">
          <input
            type="text"
            value={rawInclude}
            onChange={(e) => setRawInclude(e.target.value)}
            placeholder="Filter (case-insensitive substring across all fields)"
            aria-invalid={!includeValid}
            className={`w-full rounded-md bg-panel-elev border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info ${
              includeValid ? 'border-panel-border' : 'border-danger'
            }`}
          />
          {!includeValid && (
            <p className="text-xs text-danger mt-1">
              Only letters, digits and <span className="font-mono">:./_-</span> allowed (max 64 chars).
            </p>
          )}
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

      {neverSynced && (
        <div className="rounded-md border border-panel-border bg-panel-elev/40 text-muted px-3 py-2 text-sm italic">
          ARP / MAC tables aren&apos;t synced automatically on device
          registration — click Refresh to populate them for the first time.
          Bigger devices can take a while.
        </div>
      )}

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
        <Panel
          title="ARP table"
          actions={
            <span className="text-xs text-muted tabular-nums">
              {arpEntries?.length ?? 0} row
              {(arpEntries?.length ?? 0) === 1 ? '' : 's'}
            </span>
          }
        >
          <EntriesTable
            entries={arpEntries}
            columns={ARP_COLUMNS}
            loading={loading}
          />
        </Panel>
        <Panel
          title="MAC table"
          actions={
            <span className="text-xs text-muted tabular-nums">
              {macEntries?.length ?? 0} row
              {(macEntries?.length ?? 0) === 1 ? '' : 's'}
            </span>
          }
        >
          <EntriesTable
            entries={macEntries}
            columns={MAC_COLUMNS}
            loading={loading}
          />
        </Panel>
      </div>
    </div>
  );
}

function EntriesTable({
  entries,
  columns,
  loading,
}: {
  entries: ArpMacEntry[] | null;
  columns: readonly string[];
  loading: boolean;
}) {
  // Drop columns that are all-null so we don't render a wasted `—` column
  // when the vendor doesn't populate it (e.g. Cisco ARP doesn't report
  // vlan; Huawei does).
  const usedColumns = useMemo(() => {
    if (!entries || entries.length === 0) return [...columns];
    return columns.filter((c) => entries.some((e) => e[c] != null));
  }, [entries, columns]);

  if (loading) {
    return <p className="text-sm italic text-muted">Loading…</p>;
  }
  if (entries === null) {
    return (
      <p className="text-sm italic text-muted">
        No data cached yet.
      </p>
    );
  }
  if (entries.length === 0) {
    return (
      <p className="text-sm italic text-muted">
        No rows match the current filter.
      </p>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="text-xs uppercase tracking-wider text-muted">
          <tr>
            {usedColumns.map((c) => (
              <th key={c} className="text-left font-medium py-1 pr-3">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="font-mono">
          {entries.map((row, i) => (
            <tr key={i} className="border-t border-panel-border">
              {usedColumns.map((c) => {
                const v = row[c];
                return (
                  <td key={c} className="py-1 pr-3 text-text">
                    {v === null || v === undefined ? '—' : String(v)}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
