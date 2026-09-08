'use client';

import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { removeGlobalConfigAclRules } from '@/services/api';
import { useJobNotifications } from '@/context/JobNotificationContext';
import { Modal } from './Modal';
import {
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
  aclName: string;
  /** Raw rule lines shown as a read-only reference so the user knows what
   *  is currently configured before writing structured rules to remove. */
  currentRuleLines: string[];
}

// DELETE /devices/{name}/global-config/acls/rules -- same body shape as
// create/add, but the listed rules are subtracted (no-op for rules that
// aren't there). The rule spec has to match what the device stored,
// including protocol / port operator / endpoint form -- there is no
// wildcard "remove by index". We show the raw rule lines from the device
// as a reference so the user can compare while building the structured
// remove request.
export function AclRemoveRulesModal({
  open,
  onClose,
  deviceName,
  aclName,
  currentRuleLines,
}: Props) {
  const queryClient = useQueryClient();
  const { trackGroupJob } = useJobNotifications();

  const [drafts, setDrafts] = useState<RuleDraft[]>([makeEmptyDraft()]);
  const [error, setError] = useState<string | null>(null);

  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    if (!open) return;
    setDrafts([makeEmptyDraft()]);
    setError(null);
  }, [open, aclName]);
  /* eslint-enable react-hooks/set-state-in-effect */

  const normalized = useMemo(() => drafts.map(draftToRule), [drafts]);
  const allComplete = normalized.every((r) => r !== null);
  const rulesReady = normalized.filter((r): r is NonNullable<typeof r> => r !== null);
  const canSubmit = rulesReady.length > 0 && allComplete;

  const mutation = useMutation({
    mutationFn: () =>
      removeGlobalConfigAclRules(deviceName, {
        name: aclName,
        rules: rulesReady,
      }),
    onSuccess: (result) => {
      trackGroupJob(
        result.group_job_id,
        `Remove rules from ACL ${aclName} on ${deviceName}`,
      );
      queryClient.invalidateQueries({
        queryKey: ['global-config', 'synced', deviceName],
      });
      onClose();
    },
    onError: (err: unknown) => {
      setError(extractMessage(err, 'Failed to queue the rule removal.'));
    },
  });

  return (
    <Modal
      open={open}
      onClose={mutation.isPending ? () => undefined : onClose}
      title={`Remove rules — ACL ${aclName} on ${deviceName}`}
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
            {mutation.isPending ? 'Applying…' : 'Remove rules'}
          </ModalPrimary>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <p className="text-xs text-muted">
          Rules that don&apos;t match anything currently in the ACL are
          silently no-op&apos;d. The spec has to match the device&apos;s
          rule exactly (same protocol, endpoint form, port operator).
        </p>

        {currentRuleLines.length > 0 && (
          <section>
            <div className="text-xs font-semibold uppercase tracking-wider text-muted mb-2">
              Current rules on device
            </div>
            <ul className="flex flex-col gap-1 text-xs font-mono rounded-md bg-panel-elev/60 border border-panel-border p-2 max-h-40 overflow-y-auto">
              {currentRuleLines.map((line, i) => (
                <li key={i} className="text-text">
                  {line}
                </li>
              ))}
            </ul>
          </section>
        )}

        <AclRuleEditor
          value={drafts}
          onChange={setDrafts}
          disabled={mutation.isPending}
        />

        {rulesReady.length === 0 && (
          <p className="text-xs text-muted">
            Add at least one rule with all fields filled to enable Remove.
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
