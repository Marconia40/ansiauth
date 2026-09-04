'use client';

import { useParams } from 'next/navigation';
import { PortsTab } from '@/components/scope/PortsTab';

export default function GroupPortsPage() {
  const params = useParams<{ id: string }>();
  const groupId = Number(params.id);
  if (!Number.isFinite(groupId)) return null;
  return <PortsTab scope={{ kind: 'group', groupId }} />;
}
