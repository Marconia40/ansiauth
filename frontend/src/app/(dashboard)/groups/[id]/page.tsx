'use client';

import { useParams } from 'next/navigation';
import { ScopeDashboard } from '@/components/scope/ScopeDashboard';

export default function GroupDashboardPage() {
  const params = useParams<{ id: string }>();
  const groupId = Number(params.id);
  if (!Number.isFinite(groupId)) return null;
  return <ScopeDashboard scope={{ kind: 'group', groupId }} />;
}
