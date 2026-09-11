'use client';

import { useParams } from 'next/navigation';
import { GlobalConfigScopeTab } from '@/components/scope/GlobalConfigScopeTab';

export default function SiteGlobalConfigPage() {
  const params = useParams<{ id: string }>();
  const siteId = Number(params.id);
  if (!Number.isFinite(siteId)) return null;
  return <GlobalConfigScopeTab scope={{ kind: 'site', siteId }} />;
}
