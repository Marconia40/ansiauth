'use client';

import { useState, useEffect, useRef } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/context/AuthContext';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { PageHeader } from '@/components/PageHeader';
import { LoadingSpinner } from '@/components/LoadingSpinner';
import { ErrorMessage } from '@/components/ErrorMessage';
import { AccessBadges } from '@/components/AccessBadges';
import {
  getUsers,
  createUser,
  updateUser,
  deleteUser,
  getSites,
  listSiteGroups,
  listGrants,
  grant as grantApi,
  revoke as revokeApi,
  setSystemAdmin,
  extractMessage,
} from '@/services/api';
import type { DeviceGroup } from '@/services/api';
import type {
  AssignmentRole,
  RoleAssignment,
  User,
  UserUpdate,
} from '@/types/user';
import type { Role } from '@/types/auth';
import type { Site } from '@/types/site';

function normalizeUsers(data: unknown): User[] {
  if (Array.isArray(data)) return data as User[];
  return [];
}

const ROLES: Role[] = ['observer', 'operator', 'admin', 'super-admin'];
const ROLE_LABELS: Record<Role, string> = {
  observer: 'Observer (read-only)',
  operator: 'Operator',
  admin: 'Admin',
  'super-admin': 'Super Admin',
};

const ASSIGNMENT_ROLES: AssignmentRole[] = ['observer', 'operator', 'admin'];

export default function UsersPage() {
  const { user: currentUser } = useAuth();
  const router = useRouter();
  const queryClient = useQueryClient();

  const [isSubmitting, setIsSubmitting] = useState(false);
  const [deletingUserId, setDeletingUserId] = useState<number | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  // ── Create form ──────────────────────────────────────────────────────────
  const [newUsername, setNewUsername] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [newRole, setNewRole] = useState<Role>('observer');

  // ── Edit form (in-row) ───────────────────────────────────────────────────
  // Post-Phase-5 the row-level ``role`` dropdown was dead UI (comment
  // below on ``handleUpdate``). The Edit action now only resets the
  // password; per-scope permissions live behind the ``Grants…`` button.
  const [editingUserId, setEditingUserId] = useState<number | null>(null);
  const [editingPassword, setEditingPassword] = useState('');

  // ── Grants modal ─────────────────────────────────────────────────────────
  const [grantsUser, setGrantsUser] = useState<User | null>(null);
  const [newGrantSiteId, setNewGrantSiteId] = useState<string>('');
  const [newGrantGroupId, setNewGrantGroupId] = useState<string>('');
  const [newGrantRole, setNewGrantRole] = useState<AssignmentRole>('observer');

  const msgTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (msgTimerRef.current !== null) {
      clearTimeout(msgTimerRef.current);
      msgTimerRef.current = null;
    }
    if (!successMessage && !errorMessage) return;
    msgTimerRef.current = setTimeout(() => {
      setSuccessMessage(null);
      setErrorMessage(null);
      msgTimerRef.current = null;
    }, 4000);
    return () => {
      if (msgTimerRef.current !== null) clearTimeout(msgTimerRef.current);
    };
  }, [successMessage, errorMessage]);

  const {
    data: usersRaw,
    isLoading,
    error: usersError,
    refetch,
    isFetching,
  } = useQuery({ queryKey: ['users'], queryFn: getUsers });
  const users = normalizeUsers(usersRaw);

  const { data: sites } = useQuery<Site[]>({ queryKey: ['sites'], queryFn: getSites });
  const siteList = sites ?? [];

  // Aggregate every user's grants in a single query so the Access column
  // can render inline chips without waiting for the Grants modal to open.
  // Small user populations don't need a batch endpoint yet — parallelising
  // per-user calls behind one React Query is enough. See
  // docs/USER_PERMISSIONS_UX_REDESIGN.md §3.5.
  const userIdsKey = users.map((u) => u.id).sort((a, b) => a - b).join(',');
  const { data: grantsByUser, isLoading: grantsLoading } = useQuery<Map<number, RoleAssignment[]>>({
    queryKey: ['grants-all', userIdsKey],
    queryFn: async () => {
      const entries = await Promise.all(
        users.map(async (u) => [u.id, await listGrants(u.id)] as const),
      );
      return new Map(entries);
    },
    enabled: users.length > 0,
  });

  // ── Grants sub-queries for the currently-open user ───────────────────────
  const { data: grants, refetch: refetchGrants } = useQuery<RoleAssignment[]>({
    queryKey: ['grants', grantsUser?.id],
    queryFn: () => listGrants(grantsUser!.id),
    enabled: grantsUser != null,
  });

  const { data: grantGroups } = useQuery<DeviceGroup[]>({
    queryKey: ['site-groups', newGrantSiteId],
    queryFn: () => listSiteGroups(Number(newGrantSiteId)),
    enabled: !!newGrantSiteId,
  });

  useEffect(() => {
    if (currentUser && currentUser.role !== 'admin' && currentUser.role !== 'super-admin') {
      router.push('/');
    }
  }, [currentUser, router]);

  if (!currentUser || (currentUser.role !== 'admin' && currentUser.role !== 'super-admin')) {
    return null;
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!newUsername.trim()) { setErrorMessage('Username is required'); return; }
    if (!newPassword.trim()) { setErrorMessage('Password is required'); return; }
    setIsSubmitting(true);
    setSuccessMessage(null);
    setErrorMessage(null);
    const username = newUsername.trim();
    try {
      await createUser({
        username,
        password: newPassword.trim(),
        is_system_admin: newRole === 'admin' || newRole === 'super-admin',
      });
      setNewUsername('');
      setNewPassword('');
      setNewRole('observer');
      await refetch();
      setSuccessMessage(
        `User ${username} created. Manage per-scope grants via the "Grants…" row action.`,
      );
    } catch (err) {
      setErrorMessage(extractMessage(err, 'Create failed'));
    } finally {
      setIsSubmitting(false);
    }
  }

  function handleEditStart(u: User) {
    setEditingUserId(u.id);
    setEditingPassword('');
    setSuccessMessage(null);
    setErrorMessage(null);
  }

  function handleEditCancel() {
    setEditingUserId(null);
    setEditingPassword('');
  }

  async function handleUpdate() {
    setIsSubmitting(true);
    setSuccessMessage(null);
    setErrorMessage(null);
    try {
      const body: UserUpdate = {};
      if (editingPassword.trim()) {
        body.password = editingPassword.trim();
      }
      await updateUser(editingUserId!, body);
      handleEditCancel();
      await refetch();
      setSuccessMessage('User updated successfully');
    } catch (err) {
      setErrorMessage(extractMessage(err, 'Operation failed'));
    } finally {
      setIsSubmitting(false);
    }
  }

  async function handleDelete(u: User) {
    if (!window.confirm(`Delete user ${u.username}?`)) return;
    setDeletingUserId(u.id);
    setIsSubmitting(true);
    setSuccessMessage(null);
    setErrorMessage(null);
    try {
      await deleteUser(u.id);
      await refetch();
      setSuccessMessage(`User ${u.username} deleted successfully`);
    } catch (err) {
      setErrorMessage(extractMessage(err, 'Delete failed'));
    } finally {
      setIsSubmitting(false);
      setDeletingUserId(null);
    }
  }

  async function handleToggleSystemAdmin(u: User) {
    const next = !u.is_system_admin;
    if (!window.confirm(
      next
        ? `Promote ${u.username} to system-admin? System-admins bypass every per-scope grant.`
        : `Demote ${u.username} from system-admin?`,
    )) return;
    setIsSubmitting(true);
    setSuccessMessage(null);
    setErrorMessage(null);
    try {
      await setSystemAdmin(u.id, next);
      await refetch();
      queryClient.invalidateQueries({ queryKey: ['grants-all'] });
      setSuccessMessage(`${u.username}: is_system_admin=${next}`);
    } catch (err) {
      setErrorMessage(extractMessage(err, 'System-admin toggle failed'));
    } finally {
      setIsSubmitting(false);
    }
  }

  async function handleAddGrant() {
    if (!grantsUser) return;
    if (!newGrantSiteId) { setErrorMessage('Pick a site'); return; }
    setIsSubmitting(true);
    setSuccessMessage(null);
    setErrorMessage(null);
    try {
      await grantApi(grantsUser.id, {
        site_id: Number(newGrantSiteId),
        device_group_id: newGrantGroupId ? Number(newGrantGroupId) : null,
        role: newGrantRole,
      });
      setNewGrantSiteId('');
      setNewGrantGroupId('');
      setNewGrantRole('observer');
      await refetchGrants();
      queryClient.invalidateQueries({ queryKey: ['grants-all'] });
      setSuccessMessage('Grant issued');
    } catch (err) {
      setErrorMessage(extractMessage(err, 'Grant failed'));
    } finally {
      setIsSubmitting(false);
    }
  }

  async function handleRevoke(g: RoleAssignment) {
    if (!grantsUser) return;
    if (!window.confirm(`Revoke ${g.role} on ${g.site_name}${g.device_group_name ? ` / ${g.device_group_name}` : ''}?`)) return;
    setIsSubmitting(true);
    setSuccessMessage(null);
    setErrorMessage(null);
    try {
      await revokeApi(grantsUser.id, g.id);
      await refetchGrants();
      queryClient.invalidateQueries({ queryKey: ['grants-all'] });
      setSuccessMessage('Grant revoked');
    } catch (err) {
      setErrorMessage(extractMessage(err, 'Revoke failed'));
    } finally {
      setIsSubmitting(false);
    }
  }

  // The JWT payload only carries `role`; every admin/super-admin becomes
  // is_system_admin=True via Phase 3's create_user hook, so role is a safe
  // proxy for UI-level "should the system-admin toggle appear" checks.
  const viewerIsSystemAdmin =
    currentUser.role === 'admin' || currentUser.role === 'super-admin';

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="User Management"
        actions={
          <button
            onClick={() => refetch()}
            disabled={isLoading || isFetching || isSubmitting}
            className="px-3 py-1.5 text-sm bg-panel border border-panel-border rounded-md hover:bg-panel-elev/60 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isFetching ? 'Refreshing...' : 'Refresh'}
          </button>
        }
      />
      <p className="text-sm text-muted mb-6">
        Manage platform users. Per-scope permissions are granted per user via
        the “Grants…” row action.
      </p>

      <form onSubmit={handleCreate} className="mb-6 space-y-2">
        <div className="flex flex-wrap gap-2 items-center">
          <input
            type="text"
            placeholder="Username"
            value={newUsername}
            onChange={(e) => setNewUsername(e.target.value)}
            disabled={isSubmitting}
            required
            className="border border-panel-border rounded-md px-3 py-1.5 text-sm w-36 focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
          />
          <input
            type="password"
            placeholder="Password"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            disabled={isSubmitting}
            required
            className="border border-panel-border rounded-md px-3 py-1.5 text-sm w-36 focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
          />
          <select
            value={newRole}
            onChange={(e) => setNewRole(e.target.value as Role)}
            disabled={isSubmitting}
            className="border border-panel-border rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
          >
            {ROLES.map((r) => (
              <option key={r} value={r}>{ROLE_LABELS[r]}</option>
            ))}
          </select>
          <button
            type="submit"
            disabled={isSubmitting || !newUsername.trim() || !newPassword.trim()}
            className="px-3 py-1.5 text-sm bg-info text-white rounded-md hover:bg-info disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isSubmitting && deletingUserId === null && editingUserId === null && !grantsUser
              ? 'Creating...'
              : 'Create User'}
          </button>
        </div>
        <p className="text-xs text-muted">
          Legacy allowed-sites checkboxes are removed — after creating the
          user, use the “Grants…” row action to grant observer/operator/admin
          per site or per group.
        </p>
      </form>

      {successMessage && (
        <div className="mb-4 text-sm text-success">✓ {successMessage}</div>
      )}
      {errorMessage && (
        <div className="mb-4">
          <ErrorMessage error={`✗ ${errorMessage}`} />
        </div>
      )}

      {isLoading ? (
        <div className="py-12 flex justify-center">
          <LoadingSpinner size="lg" />
        </div>
      ) : usersError ? (
        <div className="py-6">
          <ErrorMessage error={extractMessage(usersError, 'Could not load users')} />
          <button
            onClick={() => refetch()}
            className="mt-3 px-3 py-1.5 text-sm bg-panel border border-panel-border rounded-md hover:bg-panel-elev/60"
          >
            Retry
          </button>
        </div>
      ) : users.length === 0 ? (
        <p className="py-12 text-center text-muted/70 text-sm">No users available.</p>
      ) : (
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b border-panel-border bg-panel-elev/60">
              <th className="text-left px-4 py-2 font-medium text-text">Username</th>
              <th className="text-left px-4 py-2 font-medium text-text">Access</th>
              <th className="text-left px-4 py-2 font-medium text-text">System-admin</th>
              <th className="text-left px-4 py-2 font-medium text-text">Actions</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => {
              const isEditing = editingUserId === u.id;
              return (
                <tr key={u.id} className="border-b border-panel-border hover:bg-panel-elev/60">
                  <td className="px-4 py-2 text-text font-mono text-xs">{u.username}</td>
                  <td className="px-4 py-2 text-text">
                    <AccessBadges
                      user={u}
                      grants={grantsByUser?.get(u.id)}
                      isLoading={grantsLoading}
                      onSeeMore={() => {
                        setGrantsUser(u);
                        setNewGrantSiteId('');
                        setNewGrantGroupId('');
                        setNewGrantRole('observer');
                      }}
                    />
                  </td>
                  <td className="px-4 py-2">
                    {viewerIsSystemAdmin ? (
                      <button
                        onClick={() => handleToggleSystemAdmin(u)}
                        disabled={isSubmitting}
                        className={
                          u.is_system_admin
                            ? 'px-2 py-1 text-xs text-white bg-warning rounded hover:brightness-110 disabled:opacity-50'
                            : 'px-2 py-1 text-xs text-text border border-panel-border rounded hover:bg-panel-elev/60 disabled:opacity-50'
                        }
                      >
                        {u.is_system_admin ? 'Yes (revoke)' : 'No (promote)'}
                      </button>
                    ) : (
                      <span className="text-xs text-muted">
                        {u.is_system_admin ? 'Yes' : 'No'}
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-2">
                    {isEditing ? (
                      <div className="flex flex-wrap gap-2 items-center">
                        <input
                          type="password"
                          placeholder="New password"
                          value={editingPassword}
                          onChange={(e) => setEditingPassword(e.target.value)}
                          disabled={isSubmitting}
                          className="border border-panel-border rounded-md px-2 py-1 text-sm w-36 focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
                        />
                        <button
                          onClick={handleUpdate}
                          disabled={isSubmitting}
                          className="px-2 py-1 text-xs text-white bg-info border border-blue-600 rounded hover:bg-info disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                          {isSubmitting && deletingUserId === null ? 'Saving...' : 'Save'}
                        </button>
                        <button
                          onClick={handleEditCancel}
                          disabled={isSubmitting}
                          className="px-2 py-1 text-xs text-muted border border-panel-border rounded hover:bg-panel-elev/60 disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                          Cancel
                        </button>
                      </div>
                    ) : (
                      <div className="flex gap-2">
                        <button
                          onClick={() => {
                            setGrantsUser(u);
                            setNewGrantSiteId('');
                            setNewGrantGroupId('');
                            setNewGrantRole('observer');
                          }}
                          disabled={isSubmitting}
                          className="px-2 py-1 text-xs text-info border border-info/40 rounded hover:bg-info/10 disabled:opacity-50"
                        >
                          Grants…
                        </button>
                        <button
                          onClick={() => handleEditStart(u)}
                          disabled={isSubmitting || editingUserId !== null}
                          className="px-2 py-1 text-xs text-info border border-info/40 rounded hover:bg-info/10 disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                          Edit
                        </button>
                        <button
                          onClick={() => handleDelete(u)}
                          disabled={isSubmitting}
                          className="px-2 py-1 text-xs text-danger border border-danger/40 rounded hover:bg-danger/10 disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                          {deletingUserId === u.id ? 'Deleting...' : 'Delete'}
                        </button>
                      </div>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      {/* ── Grants modal ───────────────────────────────────────────────── */}
      {grantsUser && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 overflow-y-auto py-8">
          <div className="w-full max-w-2xl bg-panel rounded-lg shadow-xl p-6">
            <div className="flex items-start justify-between mb-4">
              <div>
                <h2 className="text-lg font-semibold text-text">
                  Grants for {grantsUser.username}
                </h2>
                <p className="text-xs text-muted mt-1">
                  A grant is one role at one scope (site or site + group). The
                  most-specific matching grant wins per resource.
                </p>
              </div>
              <button
                onClick={() => setGrantsUser(null)}
                disabled={isSubmitting}
                className="text-sm text-muted hover:text-text disabled:opacity-50"
              >
                Close
              </button>
            </div>

            {/* Existing grants */}
            <section className="mb-6">
              <h3 className="text-xs font-semibold text-muted uppercase mb-2">
                Current grants
              </h3>
              {grants && grants.length > 0 ? (
                <table className="w-full border-collapse text-sm">
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
                            disabled={isSubmitting}
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
                <p className="text-sm text-muted/70">No grants yet.</p>
              )}
            </section>

            {/* Add grant */}
            <section>
              <h3 className="text-xs font-semibold text-muted uppercase mb-2">
                Add grant
              </h3>
              <div className="flex flex-wrap gap-2 items-center">
                <select
                  value={newGrantSiteId}
                  onChange={(e) => {
                    setNewGrantSiteId(e.target.value);
                    setNewGrantGroupId('');
                  }}
                  disabled={isSubmitting}
                  className="border border-panel-border rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
                >
                  <option value="">Select site…</option>
                  {siteList
                    .filter((s) => s.kind === 'REGULAR' || viewerIsSystemAdmin)
                    .map((s) => (
                      <option key={s.id} value={s.id}>{s.name}</option>
                    ))}
                </select>
                <select
                  value={newGrantGroupId}
                  onChange={(e) => setNewGrantGroupId(e.target.value)}
                  disabled={isSubmitting || !newGrantSiteId}
                  className="border border-panel-border rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
                >
                  <option value="">Whole site (any group)</option>
                  {(grantGroups ?? []).map((g) => (
                    <option key={g.id} value={g.id}>{g.name}</option>
                  ))}
                </select>
                <select
                  value={newGrantRole}
                  onChange={(e) => setNewGrantRole(e.target.value as AssignmentRole)}
                  disabled={isSubmitting}
                  className="border border-panel-border rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
                >
                  {ASSIGNMENT_ROLES.map((r) => (
                    <option key={r} value={r}>{r}</option>
                  ))}
                </select>
                <button
                  onClick={handleAddGrant}
                  disabled={isSubmitting || !newGrantSiteId}
                  className="px-3 py-1.5 text-sm text-white bg-info rounded hover:bg-info disabled:opacity-50"
                >
                  Add
                </button>
              </div>
            </section>
          </div>
        </div>
      )}
    </div>
  );
}
