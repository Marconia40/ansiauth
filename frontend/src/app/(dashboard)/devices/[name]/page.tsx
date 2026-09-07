'use client';

import { useParams } from 'next/navigation';
import { DeviceDashboard } from '@/components/scope/DeviceDashboard';

export default function DeviceDashboardPage() {
  const params = useParams<{ name: string }>();
  const raw = params.name;
  const deviceName = typeof raw === 'string' ? decodeURIComponent(raw) : '';
  if (!deviceName) return null;
  return <DeviceDashboard deviceName={deviceName} />;
}
