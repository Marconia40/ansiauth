'use client';

import { useParams } from 'next/navigation';
import { SVITab } from '@/components/scope/SVITab';

export default function SiteSVIPage() {
  const params = useParams<{ id: string }>();
  const siteId = Number(params.id);
  if (!Number.isFinite(siteId)) return null;
  return <SVITab scope={{ kind: 'site', siteId }} />;
}
