import { refreshDevicePorts, refreshDeviceVlans } from '@/services/api';

interface Options {
  /** Called after each device finishes both refreshes. */
  onProgress?: (done: number, total: number) => void;
}

/**
 * Fires VLAN + port refresh tasks for every device in scope. Returns when
 * every dispatch has resolved (task queued, not task completed — the sync
 * itself runs asynchronously and is watched via the SyncedResource envelope's
 * `sync_in_progress` flag on the next poll).
 *
 * Errors on individual devices are swallowed so a single unreachable device
 * doesn't block the rest. The freshness metadata on the next GET will surface
 * per-device failures via `sync_error`.
 */
export async function runScopeRefresh(
  deviceNames: string[],
  options: Options = {},
): Promise<void> {
  const total = deviceNames.length;
  if (total === 0) return;

  let done = 0;
  await Promise.all(
    deviceNames.map(async (name) => {
      await Promise.allSettled([refreshDeviceVlans(name), refreshDevicePorts(name)]);
      done += 1;
      options.onProgress?.(done, total);
    }),
  );
}
