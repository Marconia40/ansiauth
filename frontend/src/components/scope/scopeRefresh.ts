import { refreshDeviceSync } from '@/services/api';

interface Options {
  /** Called after each device finishes its refresh dispatch. */
  onProgress?: (done: number, total: number) => void;
}

/**
 * Fires a combined VLAN + port + SVI refresh (`scope="all"`) for every
 * device in scope. Returns when every dispatch has resolved (task queued,
 * not task completed — the sync itself runs asynchronously and is watched
 * via the SyncedResource envelope's `sync_in_progress` flag on the next
 * poll).
 *
 * Bug real encontrado en vivo contra ansiauth.psi.unc.edu.ar: esto antes
 * llamaba a refreshDeviceVlans() + refreshDevicePorts() EN PARALELO por
 * cada device -- 2 tasks/2 locks/2 sesiones SSH separadas por cada click
 * de Refresh (el doble de lo necesario, contribuyendo a los "Connection
 * reset"/"device busy" vistos en el log de ese server), y SVIs nunca se
 * refrescaba por este camino. `refreshDeviceSync()` (scope="all") ya
 * fusiona las 3 en 1 sola task/1-2 sesiones SSH server-side
 * (`DeviceSyncService.sync_core()`) -- una sola llamada por device en vez
 * de 2-3.
 *
 * Errores en devices individuales se ignoran así un device inalcanzable no
 * bloquea al resto. La metadata de freshness en el próximo GET va a
 * mostrar el fallo per-device vía `sync_error`.
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
      await refreshDeviceSync(name).catch(() => undefined);
      done += 1;
      options.onProgress?.(done, total);
    }),
  );
}
