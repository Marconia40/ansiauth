import type { PortBatchChangeItem } from '@/types/port';
import type { PortRef } from './usePortSelection';

export interface BatchResult {
  success: number;
  failed: number;
  errors: { ref: PortRef; error: string }[];
}

interface Options {
  onProgress?: (done: number, total: number) => void;
  /** Max concurrent in-flight requests. Only meaningful for `runPortBatch()`
   * (per-port). Backend takes group-based locks so flooding it doesn't buy
   * much; cap keeps failures observable. */
  concurrency?: number;
}

function extractError(err: unknown): string {
  const e = err as {
    response?: { data?: { detail?: string; message?: string } };
    message?: string;
  } | null;
  return (
    e?.response?.data?.detail ??
    e?.response?.data?.message ??
    e?.message ??
    'Request failed'
  );
}

/**
 * Runs `executor` once per selected port with bounded concurrency, collecting
 * successes and failures without letting one bad device abort the rest.
 * Progress fires after every port completes (either result). Kept for the
 * one operation that can't go through `runPortBatchByDevice()` below —
 * `reset` (RF-PUERTO-10) has no batch equivalent, it's exclusive with every
 * other field server-side (`Puerto.reset` resets EVERYTHING to defaults,
 * doesn't compose with a `changes` list of specific fields).
 */
export async function runPortBatch(
  refs: PortRef[],
  executor: (ref: PortRef) => Promise<unknown>,
  { onProgress, concurrency = 6 }: Options = {},
): Promise<BatchResult> {
  const total = refs.length;
  let done = 0;
  const result: BatchResult = { success: 0, failed: 0, errors: [] };

  let cursor = 0;
  async function worker() {
    while (true) {
      const idx = cursor++;
      if (idx >= total) return;
      const ref = refs[idx];
      try {
        await executor(ref);
        result.success += 1;
      } catch (err) {
        result.failed += 1;
        result.errors.push({ ref, error: extractError(err) });
      } finally {
        done += 1;
        onProgress?.(done, total);
      }
    }
  }

  const workers = Array.from(
    { length: Math.min(concurrency, Math.max(1, total)) },
    worker,
  );
  await Promise.all(workers);
  return result;
}

/**
 * Runs 1 batched request per device (grouping the selection via
 * `usePortSelection().byDevice`) instead of 1 request per port — same
 * `BatchResult` shape as the old per-port `runPortBatch()` it replaces, so
 * `PortActionShell`'s progress/result UI needs no changes. A device's
 * batch is all-or-nothing server-side (1 connection, 1 job): if it fails,
 * every port selected on that device counts as failed with the same
 * error message — there's no per-port outcome to report within 1 device's
 * batch, only across devices.
 */
export async function runPortBatchByDevice(
  byDevice: Map<string, string[]>,
  buildChange: (interfaceName: string) => Omit<PortBatchChangeItem, 'interface'>,
  executor: (device: string, changes: PortBatchChangeItem[]) => Promise<unknown>,
  { onProgress }: Options = {},
): Promise<BatchResult> {
  const total = Array.from(byDevice.values()).reduce((n, ifaces) => n + ifaces.length, 0);
  let done = 0;
  const result: BatchResult = { success: 0, failed: 0, errors: [] };

  for (const [device, interfaces] of byDevice) {
    const changes = interfaces.map((interfaceName) => ({
      interface: interfaceName,
      ...buildChange(interfaceName),
    }));
    try {
      await executor(device, changes);
      result.success += interfaces.length;
    } catch (err) {
      result.failed += interfaces.length;
      const error = extractError(err);
      for (const interfaceName of interfaces) {
        result.errors.push({ ref: { device, interface: interfaceName }, error });
      }
    } finally {
      done += interfaces.length;
      onProgress?.(done, total);
    }
  }

  return result;
}
