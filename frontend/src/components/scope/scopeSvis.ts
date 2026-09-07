'use client';

import { useMemo } from 'react';
import { useQueries, useQuery } from '@tanstack/react-query';
import {
  getDevices,
  getSVIsSynced,
  type SyncedResource,
} from '@/services/api';
import type { Device } from '@/types/device';
import type { SVI, SVIListResponse } from '@/types/svi';
import type { Scope } from './ScopeDashboard';
import { SYNC_POLL_INTERVAL_MS } from '@/lib/syncPolling';

/** Una fila por (device, vlan_id). A diferencia de VLANs (que se fusionan
 * porque el mismo VLAN ID en distintos devices representa la misma
 * abstracción L2), las SVIs son interfaces L3 por device -- la SVI de VLAN
 * 10 en cisco-01 tiene sus IPs propias, distintas de la SVI de VLAN 10 en
 * huawei-01. Mostrarlas separadas evita ocultar la config real detrás de
 * un merge que no representa nada del equipo. */
export interface SviRow {
  device: string;
  vlanId: number;
  description: string | null;
  adminUp: boolean | null;
  operationalUp: boolean | null;
  ipv4: string | null;
  ipv4Secondary: string | null;
  ipv6: string | null;
  aclIn: string | null;
  aclOut: string | null;
  dhcpRelayServers: string[];
}

/** Agrega SVIs de todos los devices del scope. Reusa la query de devices
 * ya cacheada por otras tabs. En device scope: N filas (una por SVI del
 * device). En site/group/org: N × devices filas, ordenadas primero por
 * device y después por vlan_id. */
export function useScopeSvis(scope: Scope) {
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

  const sviQueries = useQueries({
    queries: deviceNames.map((name) => ({
      queryKey: ['svis', 'synced', name],
      queryFn: () => getSVIsSynced(name),
      enabled: Boolean(name),
      refetchInterval: (query: { state: { data?: SyncedResource<SVIListResponse> } }) =>
        query.state.data?.sync_in_progress ? SYNC_POLL_INTERVAL_MS : false,
    })),
  });

  const rows = useMemo<SviRow[]>(() => {
    const out: SviRow[] = [];
    deviceNames.forEach((deviceName, idx) => {
      const envelope = sviQueries[idx]?.data as
        | SyncedResource<SVIListResponse>
        | undefined;
      const svis: SVI[] = envelope?.data?.svis ?? [];
      for (const svi of svis) {
        out.push({
          device: deviceName,
          vlanId: svi.vlan_id,
          description: svi.description,
          adminUp: svi.admin_up,
          operationalUp: svi.operational_up,
          ipv4: svi.ipv4_address,
          ipv4Secondary: svi.ipv4_address_secondary,
          ipv6: svi.ipv6_address,
          aclIn: svi.acl_in,
          aclOut: svi.acl_out,
          dhcpRelayServers: svi.dhcp_relay_servers ?? [],
        });
      }
    });
    // Sort: device asc (natural), then vlan_id asc.
    out.sort((a, b) => {
      const cmp = a.device.localeCompare(b.device, undefined, { numeric: true });
      if (cmp !== 0) return cmp;
      return a.vlanId - b.vlanId;
    });
    return out;
  }, [deviceNames, sviQueries]);

  const isLoading =
    devicesQuery.isLoading || sviQueries.some((q) => q.isLoading);
  const isError =
    devicesQuery.isError || sviQueries.some((q) => q.isError);

  return { rows, devices, isLoading, isError };
}
