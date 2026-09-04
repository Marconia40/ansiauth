'use client';

import { useCallback, useMemo, useState } from 'react';

export interface PortRef {
  device: string;
  interface: string;
}

interface State {
  /** Ordered set of selected ports, keyed as `${device}::${interface}`. */
  keys: Set<string>;
  /** Last port the user clicked — drives the detail panel below the chassis. */
  lastClicked: PortRef | null;
}

function keyOf(ref: PortRef): string {
  return `${ref.device}::${ref.interface}`;
}

/** Reactive state for the multi-device port selection used by the PORTS tab. */
export function usePortSelection() {
  const [state, setState] = useState<State>({ keys: new Set(), lastClicked: null });

  const toggle = useCallback((ref: PortRef) => {
    setState((prev) => {
      const next = new Set(prev.keys);
      const k = keyOf(ref);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return { keys: next, lastClicked: ref };
    });
  }, []);

  /** Set the exact selection — used by "select all on device". */
  const setForDevice = useCallback((device: string, interfaces: string[]) => {
    setState((prev) => {
      const next = new Set(prev.keys);
      // Drop the device's existing entries, then add the requested ones.
      for (const k of Array.from(next)) {
        if (k.startsWith(`${device}::`)) next.delete(k);
      }
      for (const iface of interfaces) next.add(keyOf({ device, interface: iface }));
      return { ...prev, keys: next };
    });
  }, []);

  const clearDevice = useCallback((device: string) => {
    setState((prev) => {
      const next = new Set(prev.keys);
      for (const k of Array.from(next)) {
        if (k.startsWith(`${device}::`)) next.delete(k);
      }
      return { ...prev, keys: next };
    });
  }, []);

  const clearAll = useCallback(() => {
    setState({ keys: new Set(), lastClicked: null });
  }, []);

  const refs = useMemo<PortRef[]>(() => {
    const out: PortRef[] = [];
    for (const k of state.keys) {
      const idx = k.indexOf('::');
      if (idx === -1) continue;
      out.push({ device: k.slice(0, idx), interface: k.slice(idx + 2) });
    }
    return out;
  }, [state.keys]);

  const byDevice = useMemo(() => {
    const map = new Map<string, string[]>();
    for (const ref of refs) {
      const bucket = map.get(ref.device) ?? [];
      bucket.push(ref.interface);
      map.set(ref.device, bucket);
    }
    // Natural sort within each device.
    for (const arr of map.values()) {
      arr.sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
    }
    return map;
  }, [refs]);

  const isSelected = useCallback(
    (ref: PortRef) => state.keys.has(keyOf(ref)),
    [state.keys],
  );

  return {
    refs,
    byDevice,
    lastClicked: state.lastClicked,
    count: state.keys.size,
    deviceCount: byDevice.size,
    isSelected,
    toggle,
    setForDevice,
    clearDevice,
    clearAll,
  };
}

export type PortSelection = ReturnType<typeof usePortSelection>;
