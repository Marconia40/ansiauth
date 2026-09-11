'use client';

import { useMemo, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { setGlobalConfigSnmp, parseFieldErrors } from '@/services/api';
import { useJobNotifications } from '@/context/JobNotificationContext';
import type { SnmpUpdateRequest } from '@/types/global-config';
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

// Same batch-error smuggling trick as SVICreateModal.tsx -- a synthetic
// Error thrown from mutationFn has no `response` for parseFieldErrors() to
// read, so the first failing device's parsed field errors ride along as a
// property instead.
class SnmpBulkEditError extends Error {
  fieldErrors: Record<string, string> | null;
  constructor(message: string, fieldErrors: Record<string, string> | null) {
    super(message);
    this.fieldErrors = fieldErrors;
  }
}

interface Props {
  open: boolean;
  onClose: () => void;
  scope: Scope;
}

// Bulk version of SnmpEditModal.tsx -- same PATCH-style whole-record fields
// (version/community/trap_source/trap_host+trap_version), applied to every
// selected device via N independent single-device calls (no bulk endpoint
// exists, and none is added here -- see setGlobalConfigSnmp). Each call
// already returns its own group_job_id (group_operation_runner.encolar()
// with a 1-element device list server-side), so this is genuinely 1 job per
// device, not 1 shared job.
export function SnmpBulkEditModal({ open, onClose, scope }: Props) {
  const queryClient = useQueryClient();
  const { trackGroupJob } = useJobNotifications();

  const [version, setVersion] = useState('');
  const [community, setCommunity] = useState('');
  const [trapSource, setTrapSource] = useState('');
  const [trapHost, setTrapHost] = useState('');
  const [trapVersion, setTrapVersion] = useState('');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string> | null>(null);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);

  const trimmedVersion = version.trim();
  const trimmedCommunity = community.trim();
  const trimmedTrapSource = trapSource.trim();
  const trimmedTrapHost = trapHost.trim();
  const trimmedTrapVersion = trapVersion.trim();

  const trapPairMismatch = (trimmedTrapHost === '') !== (trimmedTrapVersion === '');
  const trapPairComplete = trimmedTrapHost !== '' && trimmedTrapVersion !== '';
  const anySet =
    trimmedVersion !== '' ||
    trimmedCommunity !== '' ||
    trimmedTrapSource !== '' ||
    trapPairComplete;

  const body: SnmpUpdateRequest = useMemo(() => {
    const out: SnmpUpdateRequest = {};
    if (trimmedVersion !== '') out.version = trimmedVersion;
    if (trimmedCommunity !== '') out.community = trimmedCommunity;
    if (trimmedTrapSource !== '') out.trap_source = trimmedTrapSource;
    if (trapPairComplete) {
      out.trap_host = trimmedTrapHost;
      out.trap_version = trimmedTrapVersion;
    }
    return out;
  }, [
    trimmedVersion,
    trimmedCommunity,
    trimmedTrapSource,
    trapPairComplete,
    trimmedTrapHost,
    trimmedTrapVersion,
  ]);

  const mutation = useMutation({
    mutationFn: async () => {
      const devices = Array.from(selected);
      setProgress({ done: 0, total: devices.length });
      let done = 0;
      const results = await Promise.allSettled(
        devices.map(async (d) => {
          try {
            return await setGlobalConfigSnmp(d, body);
          } finally {
            done += 1;
            setProgress({ done, total: devices.length });
          }
        }),
      );

      const label = `Update SNMP on ${devices.length} device(s)`;
      for (const r of results) {
        if (r.status === 'fulfilled') {
          trackGroupJob(r.value.group_job_id, label);
        }
      }

      const failed = results.filter((r) => r.status === 'rejected');
      if (failed.length > 0) {
        const firstErr = (failed[0] as PromiseRejectedResult).reason;
        throw new SnmpBulkEditError(
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
      setFieldErrors(err instanceof SnmpBulkEditError ? err.fieldErrors : null);
      setError(extractMessage(err, 'Update failed.'));
    },
  });

  function resetAndClose() {
    setVersion('');
    setCommunity('');
    setTrapSource('');
    setTrapHost('');
    setTrapVersion('');
    setSelected(new Set());
    setError(null);
    setFieldErrors(null);
    setProgress(null);
    onClose();
  }

  const canSubmit =
    anySet && !trapPairMismatch && selected.size > 0 && !mutation.isPending;

  return (
    <Modal
      open={open}
      onClose={mutation.isPending ? () => undefined : resetAndClose}
      title="Bulk edit SNMP"
      footer={
        <>
          <ModalSecondary onClick={resetAndClose} disabled={mutation.isPending}>
            Cancel
          </ModalSecondary>
          <ModalPrimary onClick={() => mutation.mutate()} disabled={!canSubmit}>
            {mutation.isPending
              ? progress
                ? `Applying ${progress.done}/${progress.total}…`
                : 'Applying…'
              : 'Apply'}
          </ModalPrimary>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <p className="text-xs text-muted">
          Applies the same SNMP settings to every selected device — leave a
          field blank to leave it untouched on each. Community is applied as
          read-only. At least one knob must be filled.
        </p>

        <FieldRow label="Version">
          <select
            value={version}
            onChange={(e) => setVersion(e.target.value)}
            className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
          >
            <option value="">Leave untouched</option>
            <option value="v1">v1</option>
            <option value="v2c">v2c</option>
            <option value="v3">v3</option>
          </select>
          <p className="text-xs text-muted mt-1">
            Only takes effect on Huawei (VRP) — selected Cisco devices have no
            separate version command and will silently ignore this.
          </p>
          <FieldError message={fieldErrors?.version} />
        </FieldRow>

        <FieldRow label="Community">
          <input
            type="text"
            value={community}
            onChange={(e) => setCommunity(e.target.value)}
            placeholder="e.g. public"
            className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
          />
          <FieldError message={fieldErrors?.community} />
        </FieldRow>

        <FieldRow label="Trap source interface">
          <input
            type="text"
            value={trapSource}
            onChange={(e) => setTrapSource(e.target.value)}
            placeholder="e.g. Loopback0"
            className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
          />
          <FieldError message={fieldErrors?.trap_source} />
        </FieldRow>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <FieldRow label="Trap host">
            <input
              type="text"
              value={trapHost}
              onChange={(e) => setTrapHost(e.target.value)}
              placeholder="e.g. 10.0.0.5"
              className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
            />
            <FieldError message={fieldErrors?.trap_host} />
          </FieldRow>
          <FieldRow label="Trap version">
            <select
              value={trapVersion}
              onChange={(e) => setTrapVersion(e.target.value)}
              className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
            >
              <option value="">—</option>
              <option value="1">1</option>
              <option value="2c">2c</option>
              <option value="3">3</option>
            </select>
            <p className="text-xs text-muted mt-1">
              IOS format (no &apos;v&apos; prefix). Selected Huawei devices
              always use v2c on their trap-host regardless of this value.
            </p>
            <FieldError message={fieldErrors?.trap_version} />
          </FieldRow>
        </div>

        {trapPairMismatch && (
          <p className="text-xs text-danger">
            Trap host and trap version must be provided together.
          </p>
        )}
        {!anySet && (
          <p className="text-xs text-muted">Fill at least one field to enable Apply.</p>
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
