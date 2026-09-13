'use client';

import { useEffect, useRef } from 'react';
import { getLastActivity, markActivity } from '@/lib/sessionActivity';

// Client idle window: 14 min — deliberately shorter than the server-side
// REFRESH_TOKEN_IDLE_MINUTES=15 so the frontend logs the user out cleanly
// before the next refresh call would 401.
const IDLE_LIMIT_MS = 14 * 60 * 1000;
// Warning modal shows up 30s before the hard cut so the user has time
// to reach for the mouse before being signed out.
const WARN_BEFORE_MS = 30 * 1000;
// Poll cadence — cheaper than a setTimeout chain and robust to OS sleep
// (setTimeout can skew by hours after resume; polling recovers on the
// next tick).
const POLL_INTERVAL_MS = 5_000;

// Events treated as "user is still here". ``passive: true`` so scroll
// perf on heavy pages doesn't degrade.
const ACTIVITY_EVENTS: (keyof WindowEventMap)[] = [
  'mousemove',
  'keydown',
  'click',
  'scroll',
  'touchstart',
];

export interface IdleTimeoutCallbacks {
  onIdle: () => void;
  onWarn?: (secondsRemaining: number) => void;
  onResume?: () => void;
}

/**
 * Fire ``onIdle`` when the user has been inactive for 14 min across all
 * tabs, and ``onWarn`` during the final 30 s so the caller can show a
 * countdown modal.
 *
 * Activity is shared with sibling tabs via ``sessionActivity`` — moving
 * the mouse in Tab B keeps Tab A alive. ``onResume`` fires once when the
 * warning was showing and activity brought the user back above the
 * threshold, so the modal can auto-dismiss.
 */
export function useIdleTimeout(
  enabled: boolean,
  { onIdle, onWarn, onResume }: IdleTimeoutCallbacks,
): void {
  const onIdleRef = useRef(onIdle);
  const onWarnRef = useRef(onWarn);
  const onResumeRef = useRef(onResume);

  useEffect(() => {
    onIdleRef.current = onIdle;
    onWarnRef.current = onWarn;
    onResumeRef.current = onResume;
  }, [onIdle, onWarn, onResume]);

  useEffect(() => {
    if (!enabled || typeof window === 'undefined') return;

    // Reset the activity baseline on mount so a stale value from a
    // previous session doesn't trigger an immediate logout.
    markActivity('local');

    const handleActivity = () => markActivity('local');
    ACTIVITY_EVENTS.forEach((ev) =>
      window.addEventListener(ev, handleActivity, { passive: true }),
    );

    let warningActive = false;
    let firedIdle = false;

    const tick = () => {
      const idleFor = Date.now() - getLastActivity();

      if (idleFor >= IDLE_LIMIT_MS) {
        if (firedIdle) return;
        firedIdle = true;
        onIdleRef.current();
        return;
      }

      if (idleFor >= IDLE_LIMIT_MS - WARN_BEFORE_MS) {
        warningActive = true;
        const secondsLeft = Math.max(0, Math.ceil((IDLE_LIMIT_MS - idleFor) / 1000));
        onWarnRef.current?.(secondsLeft);
      } else if (warningActive) {
        warningActive = false;
        onResumeRef.current?.();
      }
    };

    // Immediate tick to establish current state (usually a no-op after
    // the markActivity reset above, but keeps the modal in sync after
    // remounts).
    tick();
    const interval = window.setInterval(tick, POLL_INTERVAL_MS);

    return () => {
      window.clearInterval(interval);
      ACTIVITY_EVENTS.forEach((ev) =>
        window.removeEventListener(ev, handleActivity),
      );
    };
  }, [enabled]);
}
