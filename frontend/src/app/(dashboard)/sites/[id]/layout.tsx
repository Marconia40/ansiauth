'use client';

import { useParams } from 'next/navigation';
import { useQuery } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { getSite } from '@/services/api';
import type { Site } from '@/types/site';
import { ScopeShell } from '@/components/scope/ScopeShell';
import { SiteHeaderActions } from '@/components/scope/SiteHeaderActions';

export default function SiteScopeLayout({ children }: { children: ReactNode }) {
  const params = useParams<{ id: string }>();
  const siteId = Number(params.id);

  const { data: site } = useQuery<Site>({
    queryKey: ['site', siteId],
    queryFn: () => getSite(siteId),
    enabled: Number.isFinite(siteId),
  });

  const base = `/sites/${siteId}`;

  return (
    <ScopeShell
      crumbs={[
        { label: 'Management', href: '/' },
        { label: site?.name ?? `Site #${siteId}`, href: base },
      ]}
      tabsBase={base}
      actions={<SiteHeaderActions site={site} />}
    >
      {children}
    </ScopeShell>
  );
}
