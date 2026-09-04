'use client';

import { useMemo } from 'react';
import { useQueries, useQuery } from '@tanstack/react-query';
import {
  getDevices,
  getPortsSynced,
  getVlansSynced,
  type SyncedResource,
} from '@/services/api';
import type { Device } from '@/types/device';
import type { VlanEntry } from '@/types/vlan';
import type { PortListResponse } from '@/types/port';
import type { Scope } from './ScopeDashboard';
import { vlanPortsForDevice } from './vlanPorts';

export interface VlanRow {
  vlanId: number;
  /** Best-effort friendly name (first non-empty across devices). */
  name: string;
  /** True when the same VLAN id has different names on different devices. */
  nameDiscrepancy: boolean;
  /** All name variants seen for this VLAN id. Used for tooltips. */
  names: string[];
  /** Devices that have this VLAN configured. */
  devices: string[];
  /**
   * For device-scope views: pre-formatted port numbers (with `(U)` suffix
   * on untagged). Empty at other scopes.
   */
  ports: string[];
}

/**
 * Aggregates VLAN + port data across every device in the scope. Reuses the
 * exact query keys the dashboards use so switching between tabs never re-fetches.
 * At device scope the caller gets ports-per-VLAN inline; at group/site scope
 * the ports column is intentionally empty (drill into a device to see it).
 */
export function useScopeVlans(scope: Scope) {
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

  const vlanQueries = useQueries({
    queries: deviceNames.map((name) => ({
      queryKey: ['vlans', 'synced', name],
      queryFn: () => getVlansSynced(name),
      enabled: Boolean(name),
    })),
  });
  const portQueries = useQueries({
    queries: deviceNames.map((name) => ({
      queryKey: ['ports', 'synced', name],
      queryFn: () => getPortsSynced(name),
      enabled: Boolean(name),
    })),
  });

  const rows = useMemo<VlanRow[]>(() => {
    // Accumulator keyed on VLAN id.
    const map = new Map<
      number,
      {
        names: Set<string>;
        devices: Map<string, VlanEntry>;
      }
    >();

    deviceNames.forEach((deviceName, idx) => {
      const envelope = vlanQueries[idx]?.data as
        | SyncedResource<VlanEntry[]>
        | undefined;
      if (!envelope) return;
      for (const entry of envelope.data) {
        const bucket = map.get(entry.vlan_id) ?? {
          names: new Set<string>(),
          devices: new Map<string, VlanEntry>(),
        };
        if (entry.name) bucket.names.add(entry.name);
        bucket.devices.set(deviceName, entry);
        map.set(entry.vlan_id, bucket);
      }
    });

    // At device scope, look up port assignments per VLAN.
    const soleDeviceName =
      scope.kind === 'device' && deviceNames[0] ? deviceNames[0] : null;
    const soleDevicePorts =
      soleDeviceName !== null
        ? (portQueries[0]?.data as SyncedResource<PortListResponse> | undefined)
            ?.data?.ports ?? []
        : [];

    const out: VlanRow[] = [];
    for (const [vlanId, bucket] of map) {
      const names = Array.from(bucket.names);
      out.push({
        vlanId,
        name: names[0] ?? '',
        nameDiscrepancy: names.length > 1,
        names,
        devices: Array.from(bucket.devices.keys()).sort((a, b) =>
          a.localeCompare(b, undefined, { numeric: true }),
        ),
        ports:
          soleDeviceName !== null
            ? vlanPortsForDevice(soleDevicePorts, vlanId)
            : [],
      });
    }
    out.sort((a, b) => a.vlanId - b.vlanId);
    return out;
  }, [deviceNames, vlanQueries, portQueries, scope]);

  const isLoading =
    devicesQuery.isLoading ||
    vlanQueries.some((q) => q.isLoading) ||
    (scope.kind === 'device' && portQueries.some((q) => q.isLoading));

  const isError =
    devicesQuery.isError || vlanQueries.some((q) => q.isError);

  return {
    rows,
    devices,
    isLoading,
    isError,
  };
}
