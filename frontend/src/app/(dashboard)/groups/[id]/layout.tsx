'use client';

import { useParams } from 'next/navigation';
import { useQuery } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { getDeviceGroups } from '@/services/api';
import type { DeviceGroup } from '@/services/api';
import { ScopeShell } from '@/components/scope/ScopeShell';

export default function GroupScopeLayout({ children }: { children: ReactNode }) {
  const params = useParams<{ id: string }>();
  const groupId = Number(params.id);

  // The backend doesn't expose /device-groups/{id}; the list endpoint is cheap
  // enough that we pick the row we need. React Query dedupes across shells.
  const { data: group } = useQuery<DeviceGroup | undefined>({
    queryKey: ['groups', 'find', groupId],
    queryFn: async () => {
      const all = await getDeviceGroups();
      return all.find((g) => g.id === groupId);
    },
    enabled: Number.isFinite(groupId),
  });

  const base = `/groups/${groupId}`;

  return (
    <ScopeShell
      crumbs={[
        { label: 'Management', href: '/' },
        group?.site_id
          ? { label: group.site_name ?? `Site #${group.site_id}`, href: `/sites/${group.site_id}` }
          : { label: 'Site' },
        { label: group?.name ?? `Group #${groupId}`, href: base },
      ]}
      tabsBase={base}
    >
      {children}
    </ScopeShell>
  );
}
