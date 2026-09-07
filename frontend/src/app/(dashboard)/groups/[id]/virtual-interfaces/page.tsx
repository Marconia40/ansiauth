'use client';

import { useParams } from 'next/navigation';
import { SVITab } from '@/components/scope/SVITab';

export default function GroupSVIPage() {
  const params = useParams<{ id: string }>();
  const groupId = Number(params.id);
  if (!Number.isFinite(groupId)) return null;
  return <SVITab scope={{ kind: 'group', groupId }} />;
}
