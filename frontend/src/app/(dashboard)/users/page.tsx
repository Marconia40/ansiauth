'use client';

import { useState, useEffect, useRef } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/context/AuthContext';
import { useQuery } from '@tanstack/react-query';
import { PageHeader } from '@/components/PageHeader';
import { LoadingSpinner } from '@/components/LoadingSpinner';
import { ErrorMessage } from '@/components/ErrorMessage';
import { AccessBadges } from '@/components/AccessBadges';
import { ManageUserModal } from '@/components/ManageUserModal';
import { CreateUserModal } from '@/components/CreateUserModal';
import {
  getUsers,
  deleteUser,
  getSites,
  listGrants,
  extractMessage,
} from '@/services/api';
import type { RoleAssignment, User } from '@/types/user';
import type { Site } from '@/types/site';

function normalizeUsers(data: unknown): User[] {
  if (Array.isArray(data)) return data as User[];
  return [];
}

export default function UsersPage() {
  const { user: currentUser } = useAuth();
  const router = useRouter();

  const [isSubmitting, setIsSubmitting] = useState(false);
  const [deletingUserId, setDeletingUserId] = useState<number | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  // ── Create flow (§3.4) ───────────────────────────────────────────────────
  // Two-step: CreateUserModal collects credentials, then ManageUserModal
  // opens automatically on the just-created user with a contextual banner.
  const [createOpen, setCreateOpen] = useState(false);
  const [justCreatedUserId, setJustCreatedUserId] = useState<number | null>(null);

  // ── Manage-user modal ────────────────────────────────────────────────────
  // Stored as an id (not a snapshot object) so the modal always sees the
  // freshest user row after a refetch — otherwise flipping system-admin
  // inside the modal would leave its own copy stale until reopened.
  const [manageUserId, setManageUserId] = useState<number | null>(null);

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

  const manageUser =
    manageUserId != null ? users.find((u) => u.id === manageUserId) ?? null : null;

  // Aggregate every user's grants in a single query so the Access column
  // can render inline chips without waiting for the modal to open. Small
  // user populations don't need a batch endpoint yet — parallelising
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

  useEffect(() => {
    if (currentUser && currentUser.role !== 'admin' && currentUser.role !== 'super-admin') {
      router.push('/');
    }
  }, [currentUser, router]);

  if (!currentUser || (currentUser.role !== 'admin' && currentUser.role !== 'super-admin')) {
    return null;
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

  async function handleUserCreated(u: User) {
    setCreateOpen(false);
    await refetch();
    setJustCreatedUserId(u.id);
    setManageUserId(u.id);
    setSuccessMessage(`User ${u.username} created — configure their access.`);
  }

  function handleManageClose() {
    setManageUserId(null);
    setJustCreatedUserId(null);
  }

  // The JWT payload only carries `role`; every admin/super-admin becomes
  // is_system_admin=True via Phase 3's create_user hook, so role is a safe
  // proxy for UI-level "should the system-admin toggle appear" checks.
  const viewerIsSystemAdmin =
    currentUser.role === 'admin' || currentUser.role === 'super-admin';

  const isStep2OfCreate =
    manageUser != null && justCreatedUserId === manageUser.id;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="User Management"
        actions={
          <div className="flex gap-2">
            <button
              onClick={() => setCreateOpen(true)}
              disabled={isLoading || isFetching || isSubmitting}
              className="px-3 py-1.5 text-sm bg-info text-white rounded-md hover:brightness-110 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Create user
            </button>
            <button
              onClick={() => refetch()}
              disabled={isLoading || isFetching || isSubmitting}
              className="px-3 py-1.5 text-sm bg-panel border border-panel-border rounded-md hover:bg-panel-elev/60 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {isFetching ? 'Refreshing...' : 'Refresh'}
            </button>
          </div>
        }
      />
      <p className="text-sm text-muted mb-6">
        Manage platform users. Credentials, system-wide access, and
        per-scope grants are all managed from the row Edit action.
      </p>

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
              <th className="text-left px-4 py-2 font-medium text-text">Actions</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.id} className="border-b border-panel-border hover:bg-panel-elev/60">
                <td className="px-4 py-2 text-text font-mono text-xs">{u.username}</td>
                <td className="px-4 py-2 text-text">
                  <AccessBadges
                    user={u}
                    grants={grantsByUser?.get(u.id)}
                    isLoading={grantsLoading}
                    onSeeMore={() => setManageUserId(u.id)}
                  />
                </td>
                <td className="px-4 py-2">
                  <div className="flex gap-2">
                    <button
                      onClick={() => setManageUserId(u.id)}
                      disabled={isSubmitting}
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
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {createOpen && (
        <CreateUserModal
          onClose={() => setCreateOpen(false)}
          onCreated={handleUserCreated}
        />
      )}

      {manageUser && (
        <ManageUserModal
          user={manageUser}
          sites={siteList}
          viewerIsSystemAdmin={viewerIsSystemAdmin}
          onClose={handleManageClose}
          onUserChanged={() => {
            refetch();
          }}
          banner={
            isStep2OfCreate
              ? 'User created. Configure their access below, or close to leave them with no access for now — you can grant access later from Edit.'
              : undefined
          }
          closeLabel={isStep2OfCreate ? 'Skip — user has no access yet' : undefined}
        />
      )}
    </div>
  );
}
