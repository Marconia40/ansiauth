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
import { useScope } from '@/context/ScopeContext';
import { useMyGrants } from '@/hooks/useAuthz';
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
  const { selectedScope } = useScope();

  // For site-admin callers, the backend needs an initial_grant on POST
  // /users so the new user shows up in the scoped list. Rather than
  // asking the operator up front, we transparently bootstrap a minimum
  // observer grant on the site they were browsing (or their first admin
  // site) — the operator adjusts real permissions in step 2. System-admin
  // callers skip this entirely; the backend accepts a bare create.
  const { data: myGrants } = useMyGrants();
  const adminSiteIds =
    !currentUser?.is_system_admin && myGrants
      ? myGrants
          .filter((g) => g.device_group_id === null && g.role === 'admin')
          .map((g) => g.site_id)
      : [];
  const bootstrapSiteId = currentUser?.is_system_admin
    ? null
    : selectedScope.kind === 'site' && adminSiteIds.includes(selectedScope.siteId)
      ? selectedScope.siteId
      : adminSiteIds[0] ?? null;
  const bootstrapGrant =
    bootstrapSiteId != null
      ? ({ site_id: bootstrapSiteId, role: 'observer' as const })
      : undefined;
  const cannotBootstrap =
    !currentUser?.is_system_admin && myGrants != null && bootstrapSiteId == null;

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

  // Filter the user list to the active scope. System-admins bypass every
  // per-site check on the backend, so functionally they can access any
  // site — but they don't appear in scoped lists because they hold no
  // explicit grant on it, and rendering "every system-admin plus the
  // Site-A users" would muddle what the operator asked for. Switching
  // to "All sites" is the way to manage system-admins.
  const scopedSiteId = selectedScope.kind === 'site' ? selectedScope.siteId : null;
  const visibleUsers =
    scopedSiteId == null || !grantsByUser
      ? users
      : users.filter((u) =>
          grantsByUser.get(u.id)?.some((g) => g.site_id === scopedSiteId),
        );
  const scopedSiteName =
    scopedSiteId != null ? siteList.find((s) => s.id === scopedSiteId)?.name : null;

  useEffect(() => {
    if (currentUser && !currentUser.can_manage_users) {
      router.push('/');
    }
  }, [currentUser, router]);

  if (!currentUser || !currentUser.can_manage_users) {
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
    setSuccessMessage(
      u.reactivated
        ? `User ${u.username} reactivated (a soft-deleted account with this name existed). Old grants were cleared; configure their access below.`
        : `User ${u.username} created — configure their access.`,
    );
  }

  function handleManageClose() {
    setManageUserId(null);
    setJustCreatedUserId(null);
  }

  // The system-admin toggle inside ManageUserModal is only meaningful for
  // system-admin viewers — a site-admin who can manage users still cannot
  // promote/demote system-admins.
  const viewerIsSystemAdmin = currentUser.is_system_admin;

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
      ) : visibleUsers.length === 0 ? (
        <p className="py-12 text-center text-muted/70 text-sm">
          No users have grants on{' '}
          <strong>{scopedSiteName ?? 'this site'}</strong>. Switch the topbar
          scope to <em>All sites</em> to see the full list.
        </p>
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
            {visibleUsers.map((u) => {
              // Under a site scope, collapse the chips to just the
              // grants on that site (site-wide + any groups within it).
              // The +N more affordance still opens ManageUserModal,
              // which always shows the full picture regardless of scope.
              const rawGrants = grantsByUser?.get(u.id);
              const chipGrants =
                scopedSiteId != null && rawGrants
                  ? rawGrants.filter((g) => g.site_id === scopedSiteId)
                  : rawGrants;
              return (
              <tr key={u.id} className="border-b border-panel-border hover:bg-panel-elev/60">
                <td className="px-4 py-2 text-text font-mono text-xs">{u.username}</td>
                <td className="px-4 py-2 text-text">
                  <AccessBadges
                    user={u}
                    grants={chipGrants}
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
            );
            })}
          </tbody>
        </table>
      )}

      {createOpen && (
        <CreateUserModal
          onClose={() => setCreateOpen(false)}
          onCreated={handleUserCreated}
          bootstrapGrant={bootstrapGrant}
          cannotBootstrap={cannotBootstrap}
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
          mode={isStep2OfCreate ? 'configure-new' : 'edit'}
          initialGrantSiteId={
            isStep2OfCreate && scopedSiteId != null ? scopedSiteId : undefined
          }
        />
      )}
    </div>
  );
}
