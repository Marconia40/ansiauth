import type { PortRef } from './usePortSelection';

export interface BatchResult {
  success: number;
  failed: number;
  errors: { ref: PortRef; error: string }[];
}

interface Options {
  onProgress?: (done: number, total: number) => void;
  /** Max concurrent in-flight requests. Backend takes group-based locks so
   * flooding it doesn't buy much; cap keeps failures observable. */
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
 * Progress fires after every port completes (either result).
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
