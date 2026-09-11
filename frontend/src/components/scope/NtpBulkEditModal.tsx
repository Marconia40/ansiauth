'use client';

import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { addGlobalConfigNtp, removeGlobalConfigNtp, parseFieldErrors } from '@/services/api';
import { useJobNotifications } from '@/context/JobNotificationContext';
import type { Scope } from './ScopeDashboard';
import { Modal } from './Modal';
import { DeviceSelector } from './DeviceSelector';
import {
  FieldRow,
  FieldError,
  ModalPrimary,
  ModalSecondary,
  extractMessage,
} from './VlanCreateModal';

class NtpBulkEditError extends Error {
  fieldErrors: Record<string, string> | null;
  constructor(message: string, fieldErrors: Record<string, string> | null) {
    super(message);
    this.fieldErrors = fieldErrors;
  }
}

type Mode = 'add' | 'remove';

interface Props {
  open: boolean;
  onClose: () => void;
  scope: Scope;
}

// Bulk version of NtpEditModal.tsx. The single-device modal handles add AND
// remove side-by-side because it has one device's current server list to
// show; across N devices there's no single "current list" to anchor that
// UI, so bulk picks an explicit Add/Remove mode instead. Each selected
// device gets its own addGlobalConfigNtp/removeGlobalConfigNtp call (no
// bulk endpoint — see SnmpBulkEditModal.tsx for the shared rationale).
export function NtpBulkEditModal({ open, onClose, scope }: Props) {
  const queryClient = useQueryClient();
  const { trackGroupJob } = useJobNotifications();

  const [mode, setMode] = useState<Mode>('add');
  const [server, setServer] = useState('');
  const [prefer, setPrefer] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string> | null>(null);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);

  const trimmedServer = server.trim();

  const mutation = useMutation({
    mutationFn: async () => {
      const devices = Array.from(selected);
      setProgress({ done: 0, total: devices.length });
      let done = 0;
      const results = await Promise.allSettled(
        devices.map(async (d) => {
          try {
            return mode === 'add'
              ? await addGlobalConfigNtp(d, {
                  server: trimmedServer,
                  ...(prefer ? { prefer: true } : {}),
                })
              : await removeGlobalConfigNtp(d, { server: trimmedServer });
          } finally {
            done += 1;
            setProgress({ done, total: devices.length });
          }
        }),
      );

      const label =
        mode === 'add'
          ? `Add NTP ${trimmedServer} on ${devices.length} device(s)`
          : `Remove NTP ${trimmedServer} on ${devices.length} device(s)`;
      for (const r of results) {
        if (r.status === 'fulfilled') {
          trackGroupJob(r.value.group_job_id, label);
        }
      }

      const failed = results.filter((r) => r.status === 'rejected');
      if (failed.length > 0) {
        const firstErr = (failed[0] as PromiseRejectedResult).reason;
        throw new NtpBulkEditError(
          `${failed.length} of ${devices.length} device(s) failed: ${extractMessage(firstErr, 'Update failed.')}`,
          parseFieldErrors(firstErr),
        );
      }
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['dashboard', 'summary'] });
      resetAndClose();
    },
    onError: (err: unknown) => {
      setFieldErrors(err instanceof NtpBulkEditError ? err.fieldErrors : null);
      setError(extractMessage(err, 'Update failed.'));
    },
  });

  function resetAndClose() {
    setMode('add');
    setServer('');
    setPrefer(false);
    setSelected(new Set());
    setError(null);
    setFieldErrors(null);
    setProgress(null);
    onClose();
  }

  const canSubmit = trimmedServer !== '' && selected.size > 0 && !mutation.isPending;

  return (
    <Modal
      open={open}
      onClose={mutation.isPending ? () => undefined : resetAndClose}
      title="Bulk edit NTP servers"
      footer={
        <>
          <ModalSecondary onClick={resetAndClose} disabled={mutation.isPending}>
            Cancel
          </ModalSecondary>
          <ModalPrimary onClick={() => mutation.mutate()} disabled={!canSubmit}>
            {mutation.isPending
              ? progress
                ? `${mode === 'add' ? 'Adding' : 'Removing'} ${progress.done}/${progress.total}…`
                : 'Applying…'
              : mode === 'add'
              ? 'Add'
              : 'Remove'}
          </ModalPrimary>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <ModeToggle mode={mode} onChange={setMode} />

        <FieldRow label="Server IP">
          <input
            type="text"
            value={server}
            onChange={(e) => setServer(e.target.value)}
            placeholder="e.g. 10.0.0.5"
            className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
          />
          <FieldError message={fieldErrors?.server} />
        </FieldRow>

        {mode === 'add' && (
          <label className="flex items-center gap-2 text-sm text-text">
            <input
              type="checkbox"
              checked={prefer}
              onChange={(e) => setPrefer(e.target.checked)}
              className="accent-info"
            />
            Mark as preferred (Cisco; no confirmed effect on Huawei)
          </label>
        )}

        <FieldRow label="Target devices">
          <DeviceSelector scope={scope} value={selected} onChange={setSelected} />
        </FieldRow>

        {error && (
          <p className="text-sm text-danger border border-danger/40 bg-danger/10 rounded px-3 py-2">
            {error}
          </p>
        )}
      </div>
    </Modal>
  );
}

export function ModeToggle({
  mode,
  onChange,
}: {
  mode: Mode;
  onChange: (m: Mode) => void;
}) {
  return (
    <div className="flex items-center gap-4 text-sm">
      <label className="flex items-center gap-2 cursor-pointer">
        <input
          type="radio"
          name="bulk-edit-mode"
          checked={mode === 'add'}
          onChange={() => onChange('add')}
          className="accent-info"
        />
        Add
      </label>
      <label className="flex items-center gap-2 cursor-pointer">
        <input
          type="radio"
          name="bulk-edit-mode"
          checked={mode === 'remove'}
          onChange={() => onChange('remove')}
          className="accent-info"
        />
        Remove
      </label>
    </div>
  );
}
