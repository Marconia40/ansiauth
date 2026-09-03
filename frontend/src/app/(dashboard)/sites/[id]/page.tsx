'use client';

import { useParams } from 'next/navigation';
import { ScopeDashboard } from '@/components/scope/ScopeDashboard';

export default function SiteDashboardPage() {
  const params = useParams<{ id: string }>();
  const siteId = Number(params.id);
  if (!Number.isFinite(siteId)) return null;
  return <ScopeDashboard scope={{ kind: 'site', siteId }} />;
}
