'use client';

import { useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useAuth } from '@/context/AuthContext';
import { createSite } from '@/services/api';
import { Modal } from './Modal';
import {
  FieldRow,
  ModalPrimary,
  ModalSecondary,
  extractMessage,
} from './VlanCreateModal';

// Only system-admins may create sites (backend requires it). Site-admins
// with per-scope grants can create groups within their sites but not the
// sites themselves — that's a system-admin-only operation by design.
export function OrgHeaderActions() {
  const { user } = useAuth();
  const [open, setOpen] = useState(false);

  if (!user?.is_system_admin) return null;

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="rounded-md bg-info px-3 py-1.5 text-sm font-semibold text-white hover:brightness-110 transition"
      >
        + New site
      </button>
      {open && <CreateSiteModal onClose={() => setOpen(false)} />}
    </>
  );
}

// Mounted only while open so form state resets naturally between opens.
function CreateSiteModal({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit() {
    const trimmed = name.trim();
    if (!trimmed) {
      setError('Name is required');
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await createSite({
        name: trimmed,
        description: description.trim() || undefined,
      });
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['sites'] }),
        queryClient.invalidateQueries({ queryKey: ['sidebar', 'sites'] }),
        queryClient.invalidateQueries({ queryKey: ['dashboard', 'summary'] }),
      ]);
      onClose();
    } catch (err) {
      setError(extractMessage(err, 'Create failed'));
      setSubmitting(false);
    }
  }

  return (
    <Modal
      open
      onClose={() => {
        if (!submitting) onClose();
      }}
      title="Create site"
      footer={
        <>
          <ModalSecondary onClick={onClose} disabled={submitting}>
            Cancel
          </ModalSecondary>
          <ModalPrimary onClick={handleSubmit} disabled={submitting || !name.trim()}>
            {submitting ? 'Creating…' : 'Create site'}
          </ModalPrimary>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <FieldRow label="Name">
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            disabled={submitting}
            autoFocus
            placeholder="e.g. Buenos Aires HQ"
            className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
          />
        </FieldRow>
        <FieldRow label="Description (optional)">
          <input
            type="text"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            disabled={submitting}
            placeholder="Free-form label shown in the sidebar"
            className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
          />
        </FieldRow>
        <p className="text-xs text-muted">
          A <span className="font-semibold">Default</span> device-group is
          created automatically so devices can be registered right away.
        </p>
        {error && (
          <p className="text-sm text-danger border border-danger/40 bg-danger/10 rounded px-3 py-2">
            {error}
          </p>
        )}
      </div>
    </Modal>
  );
}
