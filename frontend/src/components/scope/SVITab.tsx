'use client';

import { useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { refreshDeviceSvis } from '@/services/api';
import type { Scope } from './ScopeDashboard';
import { Panel } from './Panel';
import { RefreshButton } from './RefreshButton';
import { SVITable } from './SVITable';
import { useScopeSvis } from './scopeSvis';

interface Props {
  scope: Scope;
  /** Only meaningful at device scope — reservado para Bloques B/C (create,
   * edit, delete) que sí precisan saber el device único destino. */
  deviceName?: string;
}

/** Bloque A -- view read-only. Los bloques B (create/delete) y C (edit
 * de description/IPv4/IPv6/ACL/DHCP) se agregan encima sin tocar esto. */
export function SVITab({ scope }: Props) {
  const queryClient = useQueryClient();
  const { rows, devices, isLoading, isError } = useScopeSvis(scope);
  const [refreshing, setRefreshing] = useState(false);

  async function handleRefresh() {
    const names = devices.map((d) => d.name);
    setRefreshing(true);
    try {
      // Encola sync en todos los devices del scope. Fire and forget --
      // React Query invalida las queries y el polling agarra
      // sync_in_progress=true hasta que baja a false.
      await Promise.all(names.map((n) => refreshDeviceSvis(n)));
    } finally {
      for (const n of names) {
        queryClient.invalidateQueries({ queryKey: ['svis', 'synced', n] });
      }
      setRefreshing(false);
    }
  }

  const emptyScope = devices.length === 0;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-end">
        <RefreshButton
          onClick={handleRefresh}
          loading={refreshing}
          disabled={emptyScope}
        />
      </div>

      <Panel title="Virtual Interfaces (SVIs)">
        <SVITable
          scope={scope}
          rows={rows}
          isLoading={isLoading}
          isError={isError}
        />
      </Panel>
    </div>
  );
}
