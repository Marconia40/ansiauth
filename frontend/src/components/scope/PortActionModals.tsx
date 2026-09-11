'use client';

import { resetPort } from '@/services/api';
import { runPortBatch, type BatchResult } from './runPortBatch';
import { PortActionShell } from './PortActionShell';
import type { PortSelection } from './usePortSelection';
import type { Port } from '@/types/port';

interface Props {
  open: boolean;
  onClose: () => void;
  selection: PortSelection;
  /** Indexed lookup: device → its Port objects — used to warn when a
   * selected port is currently up (see `anySelectedPortUp` below). */
  portsByDevice: Map<string, Port[]>;
  onDone?: (result: BatchResult) => void;
}

/** True when at least one selected port has `admin_up === true` — resetting
 * a live port doesn't just wipe its config, it can drop whatever is plugged
 * into it the moment the reset applies (native VLAN back to vendor default,
 * etc.), which is worse than resetting a port that's already down. */
function anySelectedPortUp(selection: PortSelection, portsByDevice: Map<string, Port[]>): boolean {
  for (const [device, ifaces] of selection.byDevice.entries()) {
    const ports = portsByDevice.get(device) ?? [];
    for (const iface of ifaces) {
      const port = ports.find((p) => p.name === iface);
      if (port?.admin_up === true) return true;
    }
  }
  return false;
}

/** Confirms and runs `reset` on every selected port. Kept as its own modal —
 * distinct from `PortEditModal` — because reset is non-composable server-side
 * (`Puerto.reset` wipes EVERYTHING to vendor defaults, doesn't fit in a
 * `changes` list of specific fields) so it can't ride the `/ports/batch`
 * endpoint that powers the Edit modal. Runs 1 request per port with bounded
 * concurrency via `runPortBatch()`. */
export function PortResetModal({ open, onClose, selection, portsByDevice, onDone }: Props) {
  const warnUp = anySelectedPortUp(selection, portsByDevice);
  return (
    <PortActionShell
      open={open}
      onClose={onClose}
      title="Delete port config"
      selection={selection}
      actionLabel="Reset"
      tone="danger"
      onExecute={(progress) =>
        runPortBatch(
          selection.refs,
          (ref) => resetPort(ref.device, { interface: ref.interface }),
          { onProgress: progress },
        )
      }
      onDone={onDone}
    >
      <p className="text-sm text-warning border border-warning/40 bg-warning/10 rounded px-3 py-2">
        This wipes VLAN / description / PoE / storm-control back to the vendor
        default. Not reversible.
        {warnUp && (
          <>
            {' '}At least one selected port is currently up — resetting it will
            apply immediately and may drop whatever is connected.
          </>
        )}
      </p>
    </PortActionShell>
  );
}
