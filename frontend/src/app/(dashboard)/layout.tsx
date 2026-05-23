'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/context/AuthContext';
import { AppShell } from '@/components/AppShell';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { LoadingSpinner } from '@/components/LoadingSpinner';
import { JobNotifications } from '@/components/JobNotifications';

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const { user, isInitializing } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!isInitializing && user === null) {
      router.push('/login');
    }
  }, [user, isInitializing, router]);

  if (isInitializing || user === null) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <LoadingSpinner size="lg" />
      </div>
    );
  }

  return (
    <ErrorBoundary>
      <AppShell>{children}</AppShell>
      <JobNotifications />
    </ErrorBoundary>
  );
}
