'use client';

import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { createOrUpdateGlobalConfigAcl } from '@/services/api';
import { useJobNotifications } from '@/context/JobNotificationContext';
import { Modal } from './Modal';
import {
  FieldRow,
  ModalPrimary,
  ModalSecondary,
  extractMessage,
} from './VlanCreateModal';
import {
  AclRuleEditor,
  draftToRule,
  makeEmptyDraft,
  type RuleDraft,
} from './AclRuleEditor';

interface Props {
  open: boolean;
  onClose: () => void;
  deviceName: string;
  /** When set, the name field is pre-filled and locked -- used when the
   *  parent wants "add rules to this existing ACL" semantics. When null,
   *  the user types the name and this becomes a "create ACL" flow (which
   *  the backend collapses onto the same endpoint). */
  lockedName?: string | null;
}

// POST /devices/{name}/global-config/acls (RF-GLOBAL-05). Backend collapses
// "create" and "add rules" onto one endpoint: entering the extended/advanced
// ACL context creates the ACL if it doesn't exist yet, then the listed
// rules are appended (rules already configured verbatim are no-op'd
// server-side). Only extended/advanced ACLs can be created here -- Cisco
// standard / Huawei basic have to exist beforehand; we hint that in the UI
// but let the backend be authoritative on rejection.
export function AclCreateOrAddModal({
  open,
  onClose,
  deviceName,
  lockedName,
}: Props) {
  const queryClient = useQueryClient();
  const { trackGroupJob } = useJobNotifications();

  const [name, setName] = useState('');
  const [drafts, setDrafts] = useState<RuleDraft[]>([makeEmptyDraft()]);
  const [error, setError] = useState<string | null>(null);

  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    if (!open) return;
    setName(lockedName ?? '');
    setDrafts([makeEmptyDraft()]);
    setError(null);
  }, [open, lockedName]);
  /* eslint-enable react-hooks/set-state-in-effect */

  // Validate each draft. Only complete drafts (all required fields filled)
  // are eligible for submit; incomplete drafts show "incomplete" inline
  // via the editor and block the Apply button here.
  const normalized = useMemo(() => drafts.map(draftToRule), [drafts]);
  const allComplete = normalized.every((r) => r !== null);
  const rulesReady = normalized.filter((r): r is NonNullable<typeof r> => r !== null);
  const nameOk = name.trim().length > 0;

  const canSubmit =
    nameOk && rulesReady.length > 0 && allComplete;

  const mutation = useMutation({
    mutationFn: () =>
      createOrUpdateGlobalConfigAcl(deviceName, {
        name: name.trim(),
        rules: rulesReady,
      }),
    onSuccess: (result) => {
      const label = lockedName
        ? `Add rules to ACL ${name.trim()} on ${deviceName}`
        : `Create/update ACL ${name.trim()} on ${deviceName}`;
      trackGroupJob(result.group_job_id, label);
      queryClient.invalidateQueries({
        queryKey: ['global-config', 'synced', deviceName],
      });
      onClose();
    },
    onError: (err: unknown) => {
      setError(extractMessage(err, 'Failed to queue the ACL change.'));
    },
  });

  return (
    <Modal
      open={open}
      onClose={mutation.isPending ? () => undefined : onClose}
      title={
        lockedName
          ? `Add rules — ACL ${lockedName} on ${deviceName}`
          : `Create ACL — ${deviceName}`
      }
      widthClass="w-full max-w-3xl"
      footer={
        <>
          <ModalSecondary onClick={onClose} disabled={mutation.isPending}>
            Cancel
          </ModalSecondary>
          <ModalPrimary
            onClick={() => mutation.mutate()}
            disabled={!canSubmit || mutation.isPending}
          >
            {mutation.isPending
              ? 'Applying…'
              : lockedName
              ? 'Add rules'
              : 'Create'}
          </ModalPrimary>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <FieldRow label="ACL name">
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            disabled={Boolean(lockedName) || mutation.isPending}
            placeholder="e.g. ACL_MGMT"
            className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-60"
          />
        </FieldRow>

        {!lockedName && (
          <p className="text-xs text-muted">
            Only extended (Cisco) / advanced (Huawei) ACLs can be created
            here. Standard / basic ACLs must already exist on the device.
          </p>
        )}

        <AclRuleEditor
          value={drafts}
          onChange={setDrafts}
          disabled={mutation.isPending}
        />

        {rulesReady.length === 0 && (
          <p className="text-xs text-muted">
            Add at least one rule with all fields filled to enable Apply.
          </p>
        )}

        {error && (
          <p className="text-sm text-danger border border-danger/40 bg-danger/10 rounded px-3 py-2">
            {error}
          </p>
        )}
      </div>
    </Modal>
  );
}
