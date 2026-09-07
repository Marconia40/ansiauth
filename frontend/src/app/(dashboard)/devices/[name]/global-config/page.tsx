'use client';

import { useParams } from 'next/navigation';
import { GlobalConfigTab } from '@/components/scope/GlobalConfigTab';

export default function DeviceGlobalConfigPage() {
  const params = useParams<{ name: string }>();
  const raw = params.name;
  const deviceName = typeof raw === 'string' ? decodeURIComponent(raw) : '';
  if (!deviceName) return null;
  return <GlobalConfigTab deviceName={deviceName} />;
}
