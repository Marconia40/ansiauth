'use client';

import { resetPort } from '@/services/api';
import { runPortBatch, type BatchResult } from './runPortBatch';
import { PortActionShell } from './PortActionShell';
import type { PortSelection } from './usePortSelection';

interface Props {
  open: boolean;
  onClose: () => void;
  selection: PortSelection;
  onDone?: (result: BatchResult) => void;
}

/** Confirms and runs `reset` on every selected port. Kept as its own modal —
 * distinct from `PortEditModal` — because reset is non-composable server-side
 * (`Puerto.reset` wipes EVERYTHING to vendor defaults, doesn't fit in a
 * `changes` list of specific fields) so it can't ride the `/ports/batch`
 * endpoint that powers the Edit modal. Runs 1 request per port with bounded
 * concurrency via `runPortBatch()`. */
export function PortResetModal({ open, onClose, selection, onDone }: Props) {
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
      </p>
    </PortActionShell>
  );
}
