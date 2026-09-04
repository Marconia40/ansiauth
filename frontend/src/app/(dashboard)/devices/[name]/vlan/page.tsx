'use client';

import { useParams } from 'next/navigation';
import { VlanTab } from '@/components/scope/VlanTab';

export default function DeviceVlanPage() {
  const params = useParams<{ name: string }>();
  const raw = params.name;
  const deviceName = typeof raw === 'string' ? decodeURIComponent(raw) : '';
  if (!deviceName) return null;
  return (
    <VlanTab
      scope={{ kind: 'device', deviceName }}
      deviceName={deviceName}
    />
  );
}
