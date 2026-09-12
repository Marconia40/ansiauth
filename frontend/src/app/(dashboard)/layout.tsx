'use client';

import { Suspense, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/context/AuthContext';
import { ScopeProvider } from '@/context/ScopeContext';
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
      {/* Suspense wraps ScopeProvider because it reads useSearchParams,
          which Next 16 requires be inside a Suspense boundary. */}
      <Suspense fallback={<AppShell>{children}</AppShell>}>
        <ScopeProvider>
          <AppShell>{children}</AppShell>
        </ScopeProvider>
      </Suspense>
      <JobNotifications />
    </ErrorBoundary>
  );
}
