'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { useQueryClient } from '@tanstack/react-query';
import { useAuth } from '@/context/AuthContext';
import type { Site } from '@/types/site';
import {
  createDeviceGroup,
  deleteSite,
  updateSite,
} from '@/services/api';
import { Modal } from './Modal';
import {
  FieldRow,
  ModalDanger,
  ModalPrimary,
  ModalSecondary,
  extractMessage,
} from './VlanCreateModal';

interface Props {
  site: Site | undefined;
}

type OpenModal = 'group' | 'edit' | 'delete' | null;

// Actions for a single Site scope: add device-group / edit / delete.
// Visibility gate: system-admin (super-admin). Per-scope site-admin
// support would require a `/users/me/grants` endpoint (not yet in the
// backend) so we let the backend enforce and only surface UI to
// system-admins for now.
export function SiteHeaderActions({ site }: Props) {
  const { user } = useAuth();
  const [modal, setModal] = useState<OpenModal>(null);

  if (!user?.is_system_admin || !site) return null;

  const isBaseInfra = site.kind === 'BASE_INFRASTRUCTURE';
  const close = () => setModal(null);

  return (
    <div className="flex items-center gap-2">
      <button
        type="button"
        onClick={() => setModal('group')}
        className="rounded-md bg-info px-3 py-1.5 text-sm font-semibold text-white hover:brightness-110 transition"
      >
        + New device-group
      </button>
      <button
        type="button"
        onClick={() => setModal('edit')}
        disabled={isBaseInfra}
        title={isBaseInfra ? 'Base-Infrastructure is system-managed' : 'Edit site'}
        className="rounded-md border border-panel-border bg-panel-elev px-3 py-1.5 text-sm font-semibold text-text hover:bg-panel-elev/80 disabled:opacity-40 disabled:cursor-not-allowed transition"
      >
        Edit
      </button>
      <button
        type="button"
        onClick={() => setModal('delete')}
        disabled={isBaseInfra}
        title={isBaseInfra ? 'Base-Infrastructure cannot be deleted' : 'Delete site'}
        className="rounded-md border border-danger/40 bg-danger/10 px-3 py-1.5 text-sm font-semibold text-danger hover:bg-danger/20 disabled:opacity-40 disabled:cursor-not-allowed transition"
      >
        Delete
      </button>

      {modal === 'group' && <CreateGroupModal site={site} onClose={close} />}
      {modal === 'edit' && <EditSiteModal site={site} onClose={close} />}
      {modal === 'delete' && <DeleteSiteModal site={site} onClose={close} />}
    </div>
  );
}

// ── Create Device-Group ─────────────────────────────────────────────────────

function CreateGroupModal({ site, onClose }: { site: Site; onClose: () => void }) {
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
      await createDeviceGroup({
        name: trimmed,
        description: description.trim() || undefined,
        site_id: site.id,
      });
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['device-groups'] }),
        queryClient.invalidateQueries({ queryKey: ['site-groups', site.id] }),
        queryClient.invalidateQueries({ queryKey: ['sidebar', 'groups', site.id] }),
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
      title={`Create device-group in ${site.name}`}
      footer={
        <>
          <ModalSecondary onClick={onClose} disabled={submitting}>
            Cancel
          </ModalSecondary>
          <ModalPrimary onClick={handleSubmit} disabled={submitting || !name.trim()}>
            {submitting ? 'Creating…' : 'Create group'}
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
            placeholder="e.g. Access-switches"
            className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
          />
        </FieldRow>
        <FieldRow label="Description (optional)">
          <input
            type="text"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            disabled={submitting}
            className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
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

// ── Edit Site ───────────────────────────────────────────────────────────────

function EditSiteModal({ site, onClose }: { site: Site; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState(site.name);
  const [description, setDescription] = useState(site.description ?? '');
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
      await updateSite(site.id, {
        name: trimmed,
        description: description.trim(),
      });
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['site', site.id] }),
        queryClient.invalidateQueries({ queryKey: ['sites'] }),
        queryClient.invalidateQueries({ queryKey: ['sidebar', 'sites'] }),
      ]);
      onClose();
    } catch (err) {
      setError(extractMessage(err, 'Update failed'));
      setSubmitting(false);
    }
  }

  const dirty =
    name.trim() !== site.name ||
    description.trim() !== (site.description ?? '');

  return (
    <Modal
      open
      onClose={() => {
        if (!submitting) onClose();
      }}
      title={`Edit site — ${site.name}`}
      footer={
        <>
          <ModalSecondary onClick={onClose} disabled={submitting}>
            Cancel
          </ModalSecondary>
          <ModalPrimary
            onClick={handleSubmit}
            disabled={submitting || !name.trim() || !dirty}
          >
            {submitting ? 'Saving…' : 'Save changes'}
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
            className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
          />
        </FieldRow>
        <FieldRow label="Description">
          <input
            type="text"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            disabled={submitting}
            className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
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

// ── Delete Site ─────────────────────────────────────────────────────────────

function DeleteSiteModal({ site, onClose }: { site: Site; onClose: () => void }) {
  const queryClient = useQueryClient();
  const router = useRouter();
  const [confirmName, setConfirmName] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const hasDevices = site.device_count > 0;
  const nameMatches = confirmName.trim() === site.name;

  async function handleDelete() {
    if (!nameMatches) return;
    setSubmitting(true);
    setError(null);
    try {
      await deleteSite(site.id);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['sites'] }),
        queryClient.invalidateQueries({ queryKey: ['sidebar', 'sites'] }),
        queryClient.invalidateQueries({ queryKey: ['dashboard', 'summary'] }),
      ]);
      onClose();
      router.replace('/');
    } catch (err) {
      setError(extractMessage(err, 'Delete failed'));
      setSubmitting(false);
    }
  }

  return (
    <Modal
      open
      onClose={() => {
        if (!submitting) onClose();
      }}
      title={`Delete site — ${site.name}`}
      footer={
        <>
          <ModalSecondary onClick={onClose} disabled={submitting}>
            Cancel
          </ModalSecondary>
          <ModalDanger
            onClick={handleDelete}
            disabled={submitting || !nameMatches || hasDevices}
          >
            {submitting ? 'Deleting…' : 'Delete site'}
          </ModalDanger>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        {hasDevices ? (
          <p className="text-sm text-warning border border-warning/40 bg-warning/10 rounded px-3 py-2">
            This site still owns <strong>{site.device_count}</strong> device
            {site.device_count === 1 ? '' : 's'}. Move or delete every device
            before removing the site.
          </p>
        ) : (
          <p className="text-sm text-muted">
            The site and its Default device-group will be permanently removed.
            Historical audit rows and jobs remain, but the site name will no
            longer resolve in the sidebar.
          </p>
        )}
        <FieldRow label={`Type "${site.name}" to confirm`}>
          <input
            type="text"
            value={confirmName}
            onChange={(e) => setConfirmName(e.target.value)}
            disabled={submitting || hasDevices}
            autoFocus
            className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-danger disabled:opacity-50"
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
