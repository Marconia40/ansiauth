'use client';

import { useParams } from 'next/navigation';
import { GlobalConfigScopeTab } from '@/components/scope/GlobalConfigScopeTab';

export default function GroupGlobalConfigPage() {
  const params = useParams<{ id: string }>();
  const groupId = Number(params.id);
  if (!Number.isFinite(groupId)) return null;
  return <GlobalConfigScopeTab scope={{ kind: 'group', groupId }} />;
}
