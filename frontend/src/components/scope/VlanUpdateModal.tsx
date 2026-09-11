'use client';

import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { batchVlans, parseFieldErrors } from '@/services/api';
import { useJobNotifications } from '@/context/JobNotificationContext';
import type { Scope } from './ScopeDashboard';
import type { VlanRow } from './scopeVlans';
import { Modal } from './Modal';
import { DeviceSelector } from './DeviceSelector';
import {
  FieldRow,
  ModalPrimary,
  ModalSecondary,
  extractMessage,
  invalidateVlanQueries,
} from './VlanCreateModal';

interface Props {
  open: boolean;
  onClose: () => void;
  scope: Scope;
  deviceName?: string;
  rows: VlanRow[];
}

// One editable row of the update-VLAN form. Same shape as the create
// modal's draft but keyed off an EXISTING VLAN (``vlanId`` is picked
// from a dropdown of already-configured VLANs, not typed).
interface EditDraft {
  vlanId: number | '';
  description: string;
}

function makeEmptyDraft(): EditDraft {
  return { vlanId: '', description: '' };
}

export function VlanUpdateModal({
  open,
  onClose,
  scope,
  deviceName,
  rows,
}: Props) {
  const queryClient = useQueryClient();
  const { trackGroupJob } = useJobNotifications();

  const [drafts, setDrafts] = useState<EditDraft[]>([makeEmptyDraft()]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [devicesUserTouched, setDevicesUserTouched] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string> | null>(null);

  // First-picked VLAN drives the initial device pre-selection (same
  // helpful default the single-VLAN version had). Once the user changes
  // the device set, we stop syncing so their picks aren't clobbered when
  // another row's VLAN is picked.
  const firstPicked = useMemo(() => {
    const first = drafts[0];
    if (!first || first.vlanId === '') return null;
    return rows.find((r) => r.vlanId === first.vlanId) ?? null;
  }, [drafts, rows]);

  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    if (devicesUserTouched) return;
    if (!firstPicked) {
      setSelected(new Set());
      return;
    }
    setSelected(new Set(firstPicked.devices));
  }, [firstPicked, devicesUserTouched]);
  /* eslint-enable react-hooks/set-state-in-effect */

  const effectiveSelected = useMemo(() => {
    if (scope.kind === 'device' && deviceName) return new Set([deviceName]);
    return selected;
  }, [scope, deviceName, selected]);

  const readyChanges = useMemo(
    () =>
      drafts
        .filter(
          (d): d is { vlanId: number; description: string } =>
            d.vlanId !== '' && d.description.trim().length > 0,
        )
        .map((d) => ({ vlan_id: d.vlanId, name: d.description.trim() })),
    [drafts],
  );

  const allDraftsComplete = drafts.every(
    (d) => d.vlanId !== '' && d.description.trim().length > 0,
  );
  const noDuplicateIds =
    new Set(readyChanges.map((c) => c.vlan_id)).size === readyChanges.length;

  const mutation = useMutation({
    mutationFn: async () =>
      batchVlans({
        changes: readyChanges,
        devices: Array.from(effectiveSelected),
      }),
    onSuccess: (result) => {
      const label =
        readyChanges.length === 1
          ? `Update VLAN ${readyChanges[0].vlan_id} on ${effectiveSelected.size} device(s)`
          : `Update ${readyChanges.length} VLANs on ${effectiveSelected.size} device(s)`;
      trackGroupJob(result.group_job_id, label);
      invalidateVlanQueries(queryClient);
      resetAndClose();
    },
    onError: (err: unknown) => {
      const fields = parseFieldErrors(err);
      setFieldErrors(fields);
      setError(fields ? null : extractMessage(err, 'Update failed.'));
    },
  });

  function resetAndClose() {
    setDrafts([makeEmptyDraft()]);
    setSelected(new Set());
    setDevicesUserTouched(false);
    setError(null);
    setFieldErrors(null);
    onClose();
  }

  function patchDraft(idx: number, patch: Partial<EditDraft>) {
    setDrafts((prev) =>
      prev.map((d, i) => {
        if (i !== idx) return d;
        const next = { ...d, ...patch };
        // Picking a different VLAN in this row auto-fills its
        // description with the current name — matches the single-VLAN
        // version's behaviour. If the user already typed something, we
        // still overwrite (same as before).
        if (patch.vlanId !== undefined && patch.vlanId !== '' && patch.vlanId !== d.vlanId) {
          const row = rows.find((r) => r.vlanId === patch.vlanId);
          next.description = row?.name ?? '';
        }
        return next;
      }),
    );
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

  const pickedIdsInDrafts = useMemo(
    () => new Set(drafts.map((d) => d.vlanId).filter((v): v is number => v !== '')),
    [drafts],
  );

  return (
    <Modal
      open={open}
      onClose={resetAndClose}
      title="Update VLAN(s)"
      widthClass="w-full max-w-2xl"
      footer={
        <>
          <ModalSecondary onClick={resetAndClose} disabled={mutation.isPending}>
            Cancel
          </ModalSecondary>
          <ModalPrimary onClick={() => mutation.mutate()} disabled={!canSubmit}>
            {mutation.isPending
              ? 'Saving…'
              : readyChanges.length > 1
              ? `Save ${readyChanges.length}`
              : 'Save'}
          </ModalPrimary>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <p className="text-xs text-muted">
          Update one or more existing VLANs. All changes on a device run in
          a single SSH session.
        </p>

        <div className="flex flex-col gap-3">
          {drafts.map((d, i) => {
            const rowErr = rowFieldError(fieldErrors, i);
            // A VLAN picked in another draft row shouldn't appear as
            // available here — prevents duplicate-id submission from
            // being possible via the UI.
            const availableRows = rows.filter(
              (r) => r.vlanId === d.vlanId || !pickedIdsInDrafts.has(r.vlanId),
            );
            return (
              <div
                key={i}
                className="rounded-md border border-panel-border bg-panel-elev/40 p-3 flex flex-col gap-3"
              >
                <div className="flex items-center justify-between">
                  <span className="text-xs font-semibold uppercase tracking-wider text-muted">
                    Change {i + 1}
                  </span>
                  {drafts.length > 1 && (
                    <button
                      type="button"
                      onClick={() => removeDraft(i)}
                      disabled={mutation.isPending}
                      aria-label={`Remove change ${i + 1}`}
                      className="text-danger hover:brightness-125 disabled:opacity-40 disabled:cursor-not-allowed text-lg leading-none"
                    >
                      ×
                    </button>
                  )}
                </div>

                <FieldRow label="VLAN to update">
                  <select
                    value={d.vlanId}
                    onChange={(e) =>
                      patchDraft(i, {
                        vlanId: e.target.value === '' ? '' : Number(e.target.value),
                      })
                    }
                    disabled={mutation.isPending}
                    className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
                  >
                    <option value="">— pick a VLAN —</option>
                    {availableRows.map((row) => (
                      <option key={row.vlanId} value={row.vlanId}>
                        {row.vlanId} — {row.name || '(no name)'}
                      </option>
                    ))}
                  </select>
                </FieldRow>

                {d.vlanId !== '' && (
                  <FieldRow label="Description">
                    <input
                      type="text"
                      value={d.description}
                      onChange={(e) => patchDraft(i, { description: e.target.value })}
                      disabled={mutation.isPending}
                      className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
                    />
                  </FieldRow>
                )}
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
              disabled={mutation.isPending || pickedIdsInDrafts.size >= rows.length}
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
            onChange={(next) => {
              setDevicesUserTouched(true);
              setSelected(next);
            }}
            deviceName={deviceName}
          />
          {firstPicked && !devicesUserTouched && (
            <p className="text-xs text-muted mt-1">
              Devices currently carrying VLAN {firstPicked.vlanId} are
              pre-selected. Adjust as needed for the whole batch.
            </p>
          )}
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

// Same shape as VlanCreateModal's rowFieldError -- routes a backend
// validation error on entry N of `changes` to that row.
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
