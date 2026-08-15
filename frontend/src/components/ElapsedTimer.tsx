'use client';

import { useState, useEffect } from 'react';

export function ElapsedTimer({
  startedAt,
  className,
}: {
  startedAt: string | null;
  className?: string;
}) {
  const [elapsed, setElapsed] = useState<number>(0);

  useEffect(() => {
    if (!startedAt) return;
    const start = new Date(startedAt).getTime();
    const tick = () => setElapsed(Date.now() - start);
    tick();
    const id = setInterval(tick, 500);
    return () => clearInterval(id);
  }, [startedAt]);

  if (!startedAt) return null;
  return (
    <span className={className ?? 'text-xs text-gray-400'}>
      elapsed {(elapsed / 1000).toFixed(1)}s
    </span>
  );
}
