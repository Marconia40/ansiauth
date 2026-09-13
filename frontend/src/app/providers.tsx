'use client';

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useState, type ReactNode } from 'react';
import { AuthProvider } from '@/context/AuthContext';
import { JobNotificationProvider } from '@/context/JobNotificationContext';
import { StepUpProvider } from '@/context/StepUpContext';

export function Providers({ children }: { children: ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 60 * 1000,
            retry: 1,
            refetchOnWindowFocus: false,
            // Explicit even though it's the default -- Decision 4 of
            // docs/SSH_REFRESH_PLAN.md relies on this to pause all
            // ``refetchInterval`` pollings while the tab is in the
            // background. Every ``sync_in_progress`` poller (~13 across
            // the app) inherits it via QueryClient defaults.
            refetchIntervalInBackground: false,
          },
        },
      }),
  );

  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <StepUpProvider>
          <JobNotificationProvider>{children}</JobNotificationProvider>
        </StepUpProvider>
      </AuthProvider>
    </QueryClientProvider>
  );
}
