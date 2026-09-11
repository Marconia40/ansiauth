'use client';

import { useMemo, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { batchVlans, parseFieldErrors } from '@/services/api';
import { useJobNotifications } from '@/context/JobNotificationContext';
import type { Scope } from './ScopeDashboard';
import { Modal } from './Modal';
import { DeviceSelector } from './DeviceSelector';

interface Props {
  open: boolean;
  onClose: () => void;
  scope: Scope;
  /** Only relevant at device scope. */
  deviceName?: string;
}

// One editable row in the create-VLAN form. Kept as string state for
// `vlanId` so the user can partially type / clear an invalid value
// without React re-rendering it as `NaN`, mirroring the single-VLAN
// input this modal used to have.
interface VlanDraft {
  vlanId: string;
  name: string;
}

function makeEmptyDraft(): VlanDraft {
  return { vlanId: '', name: '' };
}

// A VLAN id has to be an integer in [1, 4094]. The reserved range (1,
// 1002-1005) is still checked backend-side; we don't duplicate the list
// here since the backend rejection message is already user-friendly.
function isValidVlanId(raw: string): boolean {
  if (raw === '') return false;
  const n = Number(raw);
  return Number.isInteger(n) && n >= 1 && n <= 4094;
}

export function VlanCreateModal({ open, onClose, scope, deviceName }: Props) {
  const queryClient = useQueryClient();
  const { trackGroupJob } = useJobNotifications();

  const [drafts, setDrafts] = useState<VlanDraft[]>([makeEmptyDraft()]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string> | null>(null);

  // Device scope: the modal shouldn't offer a picker — the current device is
  // always the target. We keep `selected` in sync so the submit body includes it.
  const effectiveSelected = useMemo(() => {
    if (scope.kind === 'device' && deviceName) return new Set([deviceName]);
    return selected;
  }, [scope, deviceName, selected]);

  const readyChanges = useMemo(
    () =>
      drafts
        .filter((d) => isValidVlanId(d.vlanId) && d.name.trim().length > 0)
        .map((d) => ({ vlan_id: Number(d.vlanId), name: d.name.trim() })),
    [drafts],
  );

  const allDraftsComplete = drafts.every(
    (d) => isValidVlanId(d.vlanId) && d.name.trim().length > 0,
  );
  const noDuplicateIds =
    new Set(readyChanges.map((c) => c.vlan_id)).size === readyChanges.length;

  const mutation = useMutation({
    mutationFn: async () => {
      const devices = Array.from(effectiveSelected);
      return batchVlans({ changes: readyChanges, devices });
    },
    onSuccess: (result) => {
      const label =
        readyChanges.length === 1
          ? `Create VLAN ${readyChanges[0].vlan_id} on ${effectiveSelected.size} device(s)`
          : `Create ${readyChanges.length} VLANs on ${effectiveSelected.size} device(s)`;
      trackGroupJob(result.group_job_id, label);
      invalidateVlanQueries(queryClient);
      resetAndClose();
    },
    onError: (err: unknown) => {
      const fields = parseFieldErrors(err);
      setFieldErrors(fields);
      setError(fields ? null : extractMessage(err, 'Create failed.'));
    },
  });

  function resetAndClose() {
    setDrafts([makeEmptyDraft()]);
    setSelected(new Set());
    setError(null);
    setFieldErrors(null);
    onClose();
  }

  function patchDraft(idx: number, patch: Partial<VlanDraft>) {
    setDrafts((prev) => prev.map((d, i) => (i === idx ? { ...d, ...patch } : d)));
  }

  function addDraft() {
    setDrafts((prev) => [...prev, makeEmptyDraft()]);
  }

  function removeDraft(idx: number) {
    setDrafts((prev) => (prev.length <= 1 ? prev : prev.filter((_, i) => i !== idx)));
  }

  const canSubmit =
    readyChanges.length > 0 &&
    allDraftsComplete &&
    noDuplicateIds &&
    effectiveSelected.size > 0 &&
    !mutation.isPending;

  return (
    <Modal
      open={open}
      onClose={resetAndClose}
      title="Create VLAN(s)"
      widthClass="w-full max-w-2xl"
      footer={
        <>
          <ModalSecondary onClick={resetAndClose} disabled={mutation.isPending}>
            Cancel
          </ModalSecondary>
          <ModalPrimary onClick={() => mutation.mutate()} disabled={!canSubmit}>
            {mutation.isPending
              ? 'Creating…'
              : readyChanges.length > 1
              ? `Create ${readyChanges.length}`
              : 'Create'}
          </ModalPrimary>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <p className="text-xs text-muted">
          Add one or more VLANs. When applied to a device, all VLANs in the
          list run in a single SSH session on that device.
        </p>

        <div className="flex flex-col gap-3">
          {drafts.map((d, i) => {
            const idOk = d.vlanId === '' || isValidVlanId(d.vlanId);
            const rowErr = rowFieldError(fieldErrors, i);
            return (
              <div
                key={i}
                className="rounded-md border border-panel-border bg-panel-elev/40 p-3 flex flex-col gap-3"
              >
                <div className="flex items-center justify-between">
                  <span className="text-xs font-semibold uppercase tracking-wider text-muted">
                    VLAN {i + 1}
                  </span>
                  {drafts.length > 1 && (
                    <button
                      type="button"
                      onClick={() => removeDraft(i)}
                      disabled={mutation.isPending}
                      aria-label={`Remove VLAN ${i + 1}`}
                      className="text-danger hover:brightness-125 disabled:opacity-40 disabled:cursor-not-allowed text-lg leading-none"
                    >
                      ×
                    </button>
                  )}
                </div>

                <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                  <FieldRow label="VLAN ID">
                    <input
                      type="number"
                      min={1}
                      max={4094}
                      value={d.vlanId}
                      onChange={(e) => patchDraft(i, { vlanId: e.target.value })}
                      disabled={mutation.isPending}
                      placeholder="e.g. 10"
                      className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
                    />
                    {!idOk && (
                      <p className="text-xs text-danger mt-1">
                        VLAN id must be an integer between 1 and 4094.
                      </p>
                    )}
                  </FieldRow>
                  <FieldRow label="Name">
                    <input
                      type="text"
                      value={d.name}
                      onChange={(e) => patchDraft(i, { name: e.target.value })}
                      disabled={mutation.isPending}
                      placeholder="e.g. MANAGEMENT"
                      className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info md:col-span-2"
                    />
                  </FieldRow>
                </div>
                {rowErr && (
                  <p className="text-xs text-danger">{rowErr}</p>
                )}
              </div>
            );
          })}
          <div>
            <button
              type="button"
              onClick={addDraft}
              disabled={mutation.isPending}
              className="rounded-md border border-dashed border-panel-border px-3 py-1.5 text-xs font-semibold uppercase tracking-wider text-info hover:bg-panel-elev disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              + Add VLAN
            </button>
          </div>
          {!noDuplicateIds && (
            <p className="text-xs text-danger">
              Duplicate VLAN IDs are not allowed within the same batch.
            </p>
          )}
        </div>

        <FieldRow label="Target devices">
          <DeviceSelector
            scope={scope}
            value={effectiveSelected}
            onChange={setSelected}
            deviceName={deviceName}
          />
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

// Backend validation errors on entry N of `changes` come back with a
// `loc` like `["body","changes",2,"name"]`, keyed by parseFieldErrors()
// as `"changes.2.name"`. Same idea as AclRuleEditor's rowError().
function rowFieldError(
  fieldErrors: Record<string, string> | null | undefined,
  idx: number,
): string | null {
  if (!fieldErrors) return null;
  const prefix = `changes.${idx}.`;
  const hits = Object.entries(fieldErrors).filter(([k]) => k.startsWith(prefix));
  if (hits.length === 0) return null;
  return hits.map(([k, v]) => `${k.slice(prefix.length)}: ${v}`).join('; ');
}

// ── Reused primitives ────────────────────────────────────────────────────────

// Inline field-level validation message -- see parseFieldErrors() in
// services/api.ts. Shown right under the input it's about, so a rejected
// value is visible in the same box where it was typed instead of only in
// a generic banner at the bottom of the modal (or, before that, only in
// the Audit Logs' raw JSON). Shared here since every write modal in this
// app already imports FieldRow/ModalPrimary/etc. from this same file.
export function FieldError({ message }: { message?: string }) {
  if (!message) return null;
  return <p className="text-xs text-danger mt-1">{message}</p>;
}

export function FieldRow({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-xs font-semibold uppercase tracking-wider text-muted">
        {label}
      </span>
      {children}
    </label>
  );
}

export function ModalPrimary({
  onClick,
  disabled,
  children,
}: {
  onClick?: () => void;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="rounded-md bg-info px-4 py-1.5 text-sm font-semibold text-white hover:brightness-110 disabled:opacity-40 disabled:cursor-not-allowed transition"
    >
      {children}
    </button>
  );
}

export function ModalSecondary({
  onClick,
  disabled,
  children,
}: {
  onClick?: () => void;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="rounded-md border border-panel-border bg-panel-elev px-4 py-1.5 text-sm font-semibold text-text hover:bg-panel-elev/80 disabled:opacity-40 disabled:cursor-not-allowed transition"
    >
      {children}
    </button>
  );
}

export function ModalDanger({
  onClick,
  disabled,
  children,
}: {
  onClick?: () => void;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="rounded-md bg-danger px-4 py-1.5 text-sm font-semibold text-white hover:brightness-110 disabled:opacity-40 disabled:cursor-not-allowed transition"
    >
      {children}
    </button>
  );
}

// ── Helpers ──────────────────────────────────────────────────────────────────

export function invalidateVlanQueries(queryClient: ReturnType<typeof useQueryClient>) {
  // Invalidate every device-scoped VLAN envelope so the tables refetch. We
  // don't know exactly which devices were touched from here, but React Query
  // is happy to re-run only the queries that are actually mounted.
  queryClient.invalidateQueries({ queryKey: ['vlans', 'synced'] });
}

export function extractMessage(err: unknown, fallback: string): string {
  const e = err as {
    response?: { data?: { detail?: string; message?: string } };
    message?: string;
  } | null;
  return (
    e?.response?.data?.detail ??
    e?.response?.data?.message ??
    e?.message ??
    fallback
  );
}
