'use client';

import { useParams } from 'next/navigation';
import { VlanTab } from '@/components/scope/VlanTab';

export default function SiteVlanPage() {
  const params = useParams<{ id: string }>();
  const siteId = Number(params.id);
  if (!Number.isFinite(siteId)) return null;
  return <VlanTab scope={{ kind: 'site', siteId }} />;
}
