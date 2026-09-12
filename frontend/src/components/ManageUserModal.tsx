'use client';

import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  updateUser,
  setSystemAdmin,
  listGrants,
  listSiteGroups,
  grant as grantApi,
  revoke as revokeApi,
  extractMessage,
} from '@/services/api';
import type { DeviceGroup } from '@/services/api';
import type {
  AssignmentRole,
  RoleAssignment,
  User,
  UserUpdate,
} from '@/types/user';
import type { Site } from '@/types/site';
import { ErrorMessage } from '@/components/ErrorMessage';

const ASSIGNMENT_ROLES: AssignmentRole[] = ['observer', 'operator', 'admin'];

interface ManageUserModalProps {
  user: User;
  sites: Site[];
  viewerIsSystemAdmin: boolean;
  onClose: () => void;
  onUserChanged: () => void;
  /**
   * Optional contextual banner shown above the sections — used by the
   * two-step create flow (§3.4) to explain that closing without any
   * grants leaves the user in a zero-access state.
   */
  banner?: React.ReactNode;
  /**
   * Optional label for the close button — the create flow uses
   * ``Skip — user has no access yet`` instead of ``Close``.
   */
  closeLabel?: string;
}

/**
 * Consolidated per-user access modal — replaces the pre-Phase-5 mix of
 * a row-Edit dropdown (which did nothing), a "Grants…" side modal, and a
 * separate System-admin column button. All three now live behind the row
 * Edit action. Each section applies its change immediately against the
 * existing endpoints; there is no batched "Save changes" affordance
 * because the underlying operations (password reset, system-admin
 * toggle, grant add/revoke) are independent and audited individually.
 *
 * See docs/USER_PERMISSIONS_UX_REDESIGN.md §3.3.
 */
export function ManageUserModal({
  user,
  sites,
  viewerIsSystemAdmin,
  onClose,
  onUserChanged,
  banner,
  closeLabel,
}: ManageUserModalProps) {
  const queryClient = useQueryClient();

  const [busy, setBusy] = useState(false);
  const [sectionMessage, setSectionMessage] = useState<
    { kind: 'success' | 'error'; text: string } | null
  >(null);

  // ── Credentials section ──────────────────────────────────────────────
  const [newEmail, setNewEmail] = useState(user.email ?? '');
  const [newPassword, setNewPassword] = useState('');

  // ── Grants section ───────────────────────────────────────────────────
  const [newGrantSiteId, setNewGrantSiteId] = useState('');
  const [newGrantGroupId, setNewGrantGroupId] = useState('');
  const [newGrantRole, setNewGrantRole] = useState<AssignmentRole>('observer');

  const { data: grants, refetch: refetchGrants } = useQuery<RoleAssignment[]>({
    queryKey: ['grants', user.id],
    queryFn: () => listGrants(user.id),
  });

  const { data: grantGroups } = useQuery<DeviceGroup[]>({
    queryKey: ['site-groups', newGrantSiteId],
    queryFn: () => listSiteGroups(Number(newGrantSiteId)),
    enabled: !!newGrantSiteId,
  });

  // Guardrail (§3.3 tied to backend max-role, §2.1): if the user already
  // has a site-wide admin grant on the selected site, group-level grants
  // below admin have no effect — surface that to the operator instead of
  // letting them create dead rows.
  const siteWideAdminOnSelected =
    newGrantSiteId !== '' &&
    (grants ?? []).some(
      (g) =>
        g.site_id === Number(newGrantSiteId) &&
        g.device_group_id === null &&
        g.role === 'admin',
    );
  const isGroupScoped = newGrantGroupId !== '';
  const roleLockedToAdmin =
    siteWideAdminOnSelected && isGroupScoped && newGrantRole !== 'admin';

  function flash(kind: 'success' | 'error', text: string) {
    setSectionMessage({ kind, text });
    setTimeout(() => setSectionMessage(null), 4000);
  }

  async function handleUpdateCredentials() {
    const body: UserUpdate = {};
    const trimmedEmail = newEmail.trim();
    if (trimmedEmail !== (user.email ?? '')) {
      body.email = trimmedEmail;
    }
    if (newPassword.trim()) {
      body.password = newPassword.trim();
    }
    if (Object.keys(body).length === 0) {
      flash('error', 'Nothing to update');
      return;
    }
    setBusy(true);
    try {
      await updateUser(user.id, body);
      setNewPassword('');
      onUserChanged();
      flash('success', 'Credentials updated');
    } catch (err) {
      flash('error', extractMessage(err, 'Update failed'));
    } finally {
      setBusy(false);
    }
  }

  async function handleToggleSystemAdmin() {
    const next = !user.is_system_admin;
    const message = next
      ? `Promote ${user.username} to system-admin? System-admins bypass every per-scope grant.`
      : `Demote ${user.username} from system-admin?`;
    if (!window.confirm(message)) return;
    setBusy(true);
    try {
      await setSystemAdmin(user.id, next);
      onUserChanged();
      queryClient.invalidateQueries({ queryKey: ['grants-all'] });
      flash('success', `${user.username}: is_system_admin=${next}`);
    } catch (err) {
      flash('error', extractMessage(err, 'System-admin toggle failed'));
    } finally {
      setBusy(false);
    }
  }

  async function handleAddGrant() {
    if (!newGrantSiteId) {
      flash('error', 'Pick a site');
      return;
    }
    setBusy(true);
    try {
      await grantApi(user.id, {
        site_id: Number(newGrantSiteId),
        device_group_id: newGrantGroupId ? Number(newGrantGroupId) : null,
        role: newGrantRole,
      });
      setNewGrantSiteId('');
      setNewGrantGroupId('');
      setNewGrantRole('observer');
      await refetchGrants();
      queryClient.invalidateQueries({ queryKey: ['grants-all'] });
      flash('success', 'Grant issued');
    } catch (err) {
      flash('error', extractMessage(err, 'Grant failed'));
    } finally {
      setBusy(false);
    }
  }

  async function handleRevoke(g: RoleAssignment) {
    const label = `${g.role} on ${g.site_name ?? `#${g.site_id}`}${
      g.device_group_name ? ` / ${g.device_group_name}` : ''
    }`;
    if (!window.confirm(`Revoke ${label}?`)) return;
    setBusy(true);
    try {
      await revokeApi(user.id, g.id);
      await refetchGrants();
      queryClient.invalidateQueries({ queryKey: ['grants-all'] });
      flash('success', 'Grant revoked');
    } catch (err) {
      flash('error', extractMessage(err, 'Revoke failed'));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 overflow-y-auto py-8">
      <div className="w-full max-w-2xl bg-panel rounded-lg shadow-xl p-6">
        <div className="flex items-start justify-between mb-4">
          <div>
            <h2 className="text-lg font-semibold text-text">
              Manage user: {user.username}
            </h2>
            <p className="text-xs text-muted mt-1">
              Credentials, system-wide access, and per-scope grants all
              live here. Each section applies immediately.
            </p>
          </div>
          <button
            onClick={onClose}
            disabled={busy}
            className="text-sm text-muted hover:text-text disabled:opacity-50"
          >
            {closeLabel ?? 'Close'}
          </button>
        </div>

        {banner && (
          <div className="mb-4 px-3 py-2 rounded-md border border-info/30 bg-info/5 text-sm text-text">
            {banner}
          </div>
        )}

        {sectionMessage && (
          <div className="mb-4">
            {sectionMessage.kind === 'success' ? (
              <div className="text-sm text-success">✓ {sectionMessage.text}</div>
            ) : (
              <ErrorMessage error={`✗ ${sectionMessage.text}`} />
            )}
          </div>
        )}

        {/* ── Section 1: Credentials ───────────────────────────────── */}
        <section className="mb-6 border-b border-panel-border pb-6">
          <h3 className="text-xs font-semibold text-muted uppercase mb-3">
            Credentials
          </h3>
          <div className="flex flex-wrap gap-2 items-end">
            <div className="flex flex-col">
              <label className="text-xs text-muted mb-1">Username</label>
              <input
                type="text"
                value={user.username}
                disabled
                className="border border-panel-border rounded-md px-3 py-1.5 text-sm w-40 bg-panel-elev/60 text-muted"
              />
            </div>
            <div className="flex flex-col">
              <label className="text-xs text-muted mb-1">Email</label>
              <input
                type="email"
                value={newEmail}
                onChange={(e) => setNewEmail(e.target.value)}
                disabled={busy}
                placeholder="—"
                className="border border-panel-border rounded-md px-3 py-1.5 text-sm w-52 focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
              />
            </div>
            <div className="flex flex-col">
              <label className="text-xs text-muted mb-1">Reset password</label>
              <input
                type="password"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                disabled={busy}
                placeholder="Leave blank to keep"
                className="border border-panel-border rounded-md px-3 py-1.5 text-sm w-48 focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
              />
            </div>
            <button
              onClick={handleUpdateCredentials}
              disabled={busy}
              className="px-3 py-1.5 text-sm text-white bg-info rounded-md hover:bg-info disabled:opacity-50"
            >
              Update
            </button>
          </div>
        </section>

        {/* ── Section 2: System-wide access ────────────────────────── */}
        {viewerIsSystemAdmin && (
          <section className="mb-6 border-b border-panel-border pb-6">
            <h3 className="text-xs font-semibold text-muted uppercase mb-3">
              System-wide access
            </h3>
            <div className="flex items-start gap-3">
              <button
                onClick={handleToggleSystemAdmin}
                disabled={busy}
                className={
                  user.is_system_admin
                    ? 'px-3 py-1.5 text-xs text-white bg-warning rounded hover:brightness-110 disabled:opacity-50'
                    : 'px-3 py-1.5 text-xs text-text border border-panel-border rounded hover:bg-panel-elev/60 disabled:opacity-50'
                }
              >
                {user.is_system_admin
                  ? 'Revoke system-wide admin'
                  : 'Grant system-wide admin'}
              </button>
              <p className="text-xs text-muted flex-1">
                {user.is_system_admin
                  ? 'This user currently bypasses every per-site and per-group permission.'
                  : 'Bypasses every per-site and per-group permission. Only for platform operators.'}
              </p>
            </div>
          </section>
        )}

        {/* ── Section 3: Site & group grants ───────────────────────── */}
        <section>
          <h3 className="text-xs font-semibold text-muted uppercase mb-3">
            Site &amp; group grants
          </h3>
          <p className="text-xs text-muted mb-3">
            A grant is one role at one scope (a whole site, or a specific
            group within a site). Grants only elevate — a lower role on a
            group inside a site the user already admins has no effect.
          </p>

          {grants && grants.length > 0 ? (
            <table className="w-full border-collapse text-sm mb-4">
              <thead>
                <tr className="border-b border-panel-border bg-panel-elev/60">
                  <th className="text-left px-3 py-1.5 font-medium text-text">Site</th>
                  <th className="text-left px-3 py-1.5 font-medium text-text">Group</th>
                  <th className="text-left px-3 py-1.5 font-medium text-text">Role</th>
                  <th className="text-left px-3 py-1.5 font-medium text-text">Actions</th>
                </tr>
              </thead>
              <tbody>
                {grants.map((g) => (
                  <tr key={g.id} className="border-b border-panel-border">
                    <td className="px-3 py-1.5">{g.site_name ?? `#${g.site_id}`}</td>
                    <td className="px-3 py-1.5">
                      {g.device_group_name ?? (
                        <span className="text-muted/70 italic">whole site</span>
                      )}
                    </td>
                    <td className="px-3 py-1.5">{g.role}</td>
                    <td className="px-3 py-1.5">
                      <button
                        onClick={() => handleRevoke(g)}
                        disabled={busy}
                        className="px-2 py-0.5 text-xs text-danger border border-danger/40 rounded hover:bg-danger/10 disabled:opacity-50"
                      >
                        Revoke
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="text-sm text-muted/70 mb-4">No grants yet.</p>
          )}

          <div className="space-y-2">
            <div className="flex flex-wrap gap-2 items-center">
              <select
                value={newGrantSiteId}
                onChange={(e) => {
                  setNewGrantSiteId(e.target.value);
                  setNewGrantGroupId('');
                }}
                disabled={busy}
                className="border border-panel-border rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
              >
                <option value="">Select site…</option>
                {sites
                  .filter((s) => s.kind === 'REGULAR' || viewerIsSystemAdmin)
                  .map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name}
                    </option>
                  ))}
              </select>
              <select
                value={newGrantGroupId}
                onChange={(e) => setNewGrantGroupId(e.target.value)}
                disabled={busy || !newGrantSiteId}
                className="border border-panel-border rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
              >
                <option value="">Whole site (all current and future groups)</option>
                {(grantGroups ?? []).map((g) => (
                  <option key={g.id} value={g.id}>
                    {g.name}
                  </option>
                ))}
              </select>
              <select
                value={newGrantRole}
                onChange={(e) => setNewGrantRole(e.target.value as AssignmentRole)}
                disabled={busy}
                className="border border-panel-border rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
                title={
                  roleLockedToAdmin
                    ? 'This user is already admin on the whole site — a lower role on a group inside has no effect.'
                    : undefined
                }
              >
                {ASSIGNMENT_ROLES.map((r) => {
                  const disabled =
                    siteWideAdminOnSelected && isGroupScoped && r !== 'admin';
                  return (
                    <option key={r} value={r} disabled={disabled}>
                      {r}
                      {disabled ? ' (already admin site-wide)' : ''}
                    </option>
                  );
                })}
              </select>
              <button
                onClick={handleAddGrant}
                disabled={busy || !newGrantSiteId || roleLockedToAdmin}
                className="px-3 py-1.5 text-sm text-white bg-info rounded hover:bg-info disabled:opacity-50"
              >
                Add grant
              </button>
            </div>
            {roleLockedToAdmin && (
              <p className="text-xs text-muted italic">
                This user is already admin on the whole site — pick{' '}
                <code>admin</code> or choose a different site/group.
              </p>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}
