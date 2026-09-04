'use client';

import { useParams } from 'next/navigation';
import { SVITab } from '@/components/scope/SVITab';

export default function DeviceSVIPage() {
  const params = useParams<{ name: string }>();
  const raw = params.name;
  const deviceName = typeof raw === 'string' ? decodeURIComponent(raw) : '';
  if (!deviceName) return null;
  return (
    <SVITab
      scope={{ kind: 'device', deviceName }}
      deviceName={deviceName}
    />
  );
}
