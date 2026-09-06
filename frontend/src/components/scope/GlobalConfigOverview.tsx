'use client';

import { useState, type ReactNode } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  getGlobalConfigSynced,
  getGlobalConfigVersionSynced,
  refreshDeviceGlobalConfig,
  type SyncedResource,
} from '@/services/api';
import type {
  GlobalConfigRead,
  GlobalConfigVersionRead,
} from '@/types/global-config';
import { Panel } from './Panel';
import { RefreshButton } from './RefreshButton';

interface Props {
  deviceName: string;
}

// Read-only Overview of the device's individual global-config settings
// (hostname, SNMP, NTP, DNS, logging) + version info. Both queries share
// the same `global_config` sync scope, so 1 refresh triggers both and any
// in-progress sync polls both back to fresh. The Static Routes / ACLs /
// ARP-MAC / Logs / Running-config sub-tabs live under their own sections
// and are handled in later blocks.
export function GlobalConfigOverview({ deviceName }: Props) {
  const queryClient = useQueryClient();

  const configQuery = useQuery({
    queryKey: ['global-config', 'synced', deviceName],
    queryFn: () => getGlobalConfigSynced(deviceName),
    enabled: Boolean(deviceName),
    refetchInterval: (query: {
      state: { data?: SyncedResource<GlobalConfigRead> };
    }) => (query.state.data?.sync_in_progress ? 2000 : false),
  });

  const versionQuery = useQuery({
    queryKey: ['global-config', 'version', 'synced', deviceName],
    queryFn: () => getGlobalConfigVersionSynced(deviceName),
    enabled: Boolean(deviceName),
    refetchInterval: (query: {
      state: { data?: SyncedResource<GlobalConfigVersionRead> };
    }) => (query.state.data?.sync_in_progress ? 2000 : false),
  });

  const config = configQuery.data?.data;
  const version = versionQuery.data?.data;

  const inProgress = Boolean(
    configQuery.data?.sync_in_progress || versionQuery.data?.sync_in_progress,
  );
  const syncedAt =
    configQuery.data?.synced_at ?? versionQuery.data?.synced_at ?? null;
  const syncError =
    configQuery.data?.sync_error ?? versionQuery.data?.sync_error ?? null;

  const [refreshing, setRefreshing] = useState(false);
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

  const loading = configQuery.isLoading || versionQuery.isLoading;
  const neverSynced =
    !loading &&
    !inProgress &&
    !syncError &&
    syncedAt === null &&
    !config &&
    !version;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-end">
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

      <Panel title="About device">
        <ul className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-2 text-sm">
          <DetailRow label="HOSTNAME" value={renderScalar(config?.hostname)} />
          <DetailRow label="MODEL" value={renderScalar(version?.model)} />
          <DetailRow
            label="SOFTWARE VERSION"
            value={renderScalar(version?.software_version)}
          />
          <DetailRow label="UPTIME" value={renderScalar(version?.uptime)} />
        </ul>
      </Panel>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Panel title="SNMP">
          <ul className="flex flex-col gap-2 text-sm">
            <DetailRow label="ENABLED" value={renderBool(config?.snmp.enabled)} />
            <DetailRow label="VERSION" value={renderScalar(config?.snmp.version)} />
            <DetailRow
              label="COMMUNITY"
              value={renderScalar(config?.snmp.community)}
            />
            <DetailRow
              label="PERMISSION"
              value={renderScalar(config?.snmp.permission)}
            />
            <DetailRow
              label="TRAP HOSTS"
              value={renderList(config?.snmp.trap_hosts)}
            />
          </ul>
        </Panel>

        <Panel title="NTP">
          <ServerList servers={config?.ntp.servers ?? null} />
        </Panel>

        <Panel title="DNS">
          <ServerList servers={config?.dns.servers ?? null} />
        </Panel>

        <Panel title="Logging">
          <ul className="flex flex-col gap-2 text-sm">
            <DetailRow label="LEVEL" value={renderScalar(config?.logging.level)} />
          </ul>
          <div className="mt-3">
            <div className="text-xs font-semibold text-muted uppercase tracking-wider mb-1">
              Servers
            </div>
            <ServerList servers={config?.logging.servers ?? null} />
          </div>
        </Panel>
      </div>
    </div>
  );
}

// ── Small render helpers ─────────────────────────────────────────────────────

function renderScalar(value: string | number | null | undefined): ReactNode {
  if (value === null || value === undefined || value === '') return '—';
  return String(value);
}

function renderBool(value: boolean | null | undefined): ReactNode {
  if (value === true) return 'Yes';
  if (value === false) return 'No';
  return '—';
}

function renderList(items: string[] | null | undefined): ReactNode {
  if (!items || items.length === 0) return '—';
  return items.join(', ');
}

function ServerList({ servers }: { servers: string[] | null }) {
  if (!servers || servers.length === 0) {
    return <p className="text-sm italic text-muted">None configured.</p>;
  }
  return (
    <ul className="flex flex-col gap-1 text-sm font-mono">
      {servers.map((s, i) => (
        <li key={`${s}-${i}`} className="text-text">
          {s}
        </li>
      ))}
    </ul>
  );
}

function DetailRow({
  label,
  value,
}: {
  label: string;
  value: ReactNode;
}) {
  return (
    <li className="flex items-start gap-2">
      <span className="text-info leading-6" aria-hidden>
        •
      </span>
      <span className="font-semibold text-muted uppercase tracking-wider text-xs mt-0.5 w-40 shrink-0">
        {label} =
      </span>
      <span className="text-sm text-text break-words">{value}</span>
    </li>
  );
}
