'use client';

import { useParams } from 'next/navigation';
import { PortsTab } from '@/components/scope/PortsTab';

export default function DevicePortsPage() {
  const params = useParams<{ name: string }>();
  const raw = params.name;
  const deviceName = typeof raw === 'string' ? decodeURIComponent(raw) : '';
  if (!deviceName) return null;
  return <PortsTab scope={{ kind: 'device', deviceName }} />;
}
