'use client';

import { useParams } from 'next/navigation';
import { VlanTab } from '@/components/scope/VlanTab';

export default function GroupVlanPage() {
  const params = useParams<{ id: string }>();
  const groupId = Number(params.id);
  if (!Number.isFinite(groupId)) return null;
  return <VlanTab scope={{ kind: 'group', groupId }} />;
}
