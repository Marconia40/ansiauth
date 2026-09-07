'use client';

import { useParams } from 'next/navigation';
import { PortsTab } from '@/components/scope/PortsTab';

export default function SitePortsPage() {
  const params = useParams<{ id: string }>();
  const siteId = Number(params.id);
  if (!Number.isFinite(siteId)) return null;
  return <PortsTab scope={{ kind: 'site', siteId }} />;
}
