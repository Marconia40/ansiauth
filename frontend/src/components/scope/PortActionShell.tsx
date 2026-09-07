'use client';

import { useState, type ReactNode } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { Modal } from './Modal';
import {
  ModalDanger,
  ModalPrimary,
  ModalSecondary,
} from './VlanCreateModal';
import type { BatchResult } from './runPortBatch';
import type { PortSelection } from './usePortSelection';

interface Props {
  open: boolean;
  onClose: () => void;
  title: string;
  selection: PortSelection;
  actionLabel: string;
  tone?: 'default' | 'danger';
  /** Body form — inputs the user needs to fill before executing. */
  children?: ReactNode;
  /** Called on Confirm. Must return a BatchResult; the shell handles the UI. */
  onExecute: (progress: (done: number, total: number) => void) => Promise<BatchResult>;
  /** Whether the Confirm button is enabled (e.g., form is valid). Defaults to true. */
  canExecute?: boolean;
  /** Called after a successful run so the parent can toast / invalidate. */
  onDone?: (result: BatchResult) => void;
}

/**
 * Shared shell for every "run action on N selected ports" modal. Owns the
 * confirmation flow: idle → running (with progress) → done (result summary).
 * Concrete modals inject the form (`children`) and an `onExecute` that
 * translates form state into per-port calls.
 */
export function PortActionShell({
  open,
  onClose,
  title,
  selection,
  actionLabel,
  tone = 'default',
  children,
  onExecute,
  canExecute = true,
  onDone,
}: Props) {
  const queryClient = useQueryClient();
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(
    null,
  );
  const [result, setResult] = useState<BatchResult | null>(null);
  const [running, setRunning] = useState(false);

  function reset() {
    setProgress(null);
    setResult(null);
    setRunning(false);
  }

  async function handleConfirm() {
    setRunning(true);
    setResult(null);
    setProgress({ done: 0, total: selection.refs.length });
    try {
      const res = await onExecute((done, total) => setProgress({ done, total }));
      setResult(res);
      // Invalidate port envelopes for every touched device so the tables and
      // chassis refresh with the post-action state.
      const devices = new Set(selection.refs.map((r) => r.device));
      for (const d of devices) {
        queryClient.invalidateQueries({ queryKey: ['ports', 'synced', d] });
      }
      onDone?.(res);
    } finally {
      setRunning(false);
    }
  }

  function handleClose() {
    reset();
    onClose();
  }

  const ConfirmBtn = tone === 'danger' ? ModalDanger : ModalPrimary;

  return (
    <Modal
      open={open}
      onClose={handleClose}
      title={title}
      widthClass="w-full max-w-lg"
      footer={
        result ? (
          <ModalSecondary onClick={handleClose}>Close</ModalSecondary>
        ) : (
          <>
            <ModalSecondary onClick={handleClose} disabled={running}>
              Cancel
            </ModalSecondary>
            <ConfirmBtn
              onClick={handleConfirm}
              disabled={!canExecute || running || selection.count === 0}
            >
              {running
                ? progress
                  ? `${actionLabel} ${progress.done}/${progress.total}…`
                  : `${actionLabel}…`
                : `${actionLabel} ${selection.count} port${
                    selection.count === 1 ? '' : 's'
                  }`}
            </ConfirmBtn>
          </>
        )
      }
    >
      <div className="flex flex-col gap-4">
        <SelectionBanner selection={selection} />
        {children}
        {result && <ResultView result={result} />}
      </div>
    </Modal>
  );
}

function SelectionBanner({ selection }: { selection: PortSelection }) {
  return (
    <div className="rounded-md border border-panel-border bg-panel-elev/60 px-3 py-2 text-sm">
      Operating on{' '}
      <span className="font-semibold text-text tabular-nums">{selection.count}</span>{' '}
      port{selection.count === 1 ? '' : 's'} across{' '}
      <span className="font-semibold text-text tabular-nums">
        {selection.deviceCount}
      </span>{' '}
      device{selection.deviceCount === 1 ? '' : 's'}.
    </div>
  );
}

function ResultView({ result }: { result: BatchResult }) {
  const total = result.success + result.failed;
  const allOk = result.failed === 0;
  return (
    <div
      className={`rounded-md border px-3 py-2 text-sm ${
        allOk
          ? 'border-success/60 bg-success/10 text-success'
          : 'border-warning/60 bg-warning/10 text-warning'
      }`}
    >
      <p className="font-semibold">
        {allOk ? 'All ports succeeded' : 'Finished with errors'}:{' '}
        <span className="tabular-nums">
          {result.success}/{total}
        </span>{' '}
        OK, <span className="tabular-nums">{result.failed}</span> failed.
      </p>
      {result.errors.length > 0 && (
        <ul className="mt-2 max-h-32 overflow-y-auto space-y-1 text-xs text-text">
          {result.errors.map(({ ref, error }) => (
            <li key={`${ref.device}::${ref.interface}`}>
              <span className="font-mono">
                {ref.device} · {ref.interface}
              </span>{' '}
              — {error}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
