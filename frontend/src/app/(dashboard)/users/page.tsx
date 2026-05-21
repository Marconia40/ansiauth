'use client';

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { PageHeader } from '@/components/PageHeader';
import { LoadingSpinner } from '@/components/LoadingSpinner';
import { ErrorMessage } from '@/components/ErrorMessage';
import { RequireRole } from '@/components/RequireRole';
import { getUsers, createUser, updateUser, deleteUser } from '@/services/api';
import type { User, UserUpdate } from '@/types/user';
import type { Role } from '@/types/auth';

function extractMessage(error: unknown, fallback: string): string {
  const e = error as { response?: { data?: { detail?: string } }; message?: string } | null;
  return e?.response?.data?.detail ?? e?.message ?? fallback;
}

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

export default function UsersPage() {
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  // Create form
  const [newUsername, setNewUsername] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [newRole, setNewRole] = useState<Role>('observer');

  // Edit state
  const [editingUserId, setEditingUserId] = useState<number | null>(null);
  const [editingUsername, setEditingUsername] = useState('');
  const [editingRole, setEditingRole] = useState<Role>('observer');
  const [editingPassword, setEditingPassword] = useState('');

  const {
    data: usersRaw,
    isLoading,
    error: usersError,
    refetch,
    isFetching,
  } = useQuery({
    queryKey: ['users'],
    queryFn: getUsers,
  });

  const users = normalizeUsers(usersRaw);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!newUsername.trim() || !newPassword.trim()) return;
    setIsSubmitting(true);
    setSuccessMessage(null);
    setErrorMessage(null);
    try {
      await createUser({
        username: newUsername.trim(),
        password: newPassword.trim(),
        role: newRole,
      });
      setNewUsername('');
      setNewPassword('');
      setNewRole('observer');
      await refetch();
      setSuccessMessage('User created successfully');
    } catch (err) {
      setErrorMessage(extractMessage(err, 'Create failed'));
    } finally {
      setIsSubmitting(false);
    }
  }

  function handleEditStart(user: User) {
    setEditingUserId(user.id);
    setEditingUsername(user.username);
    setEditingRole(user.role as Role);
    setEditingPassword('');
    setSuccessMessage(null);
    setErrorMessage(null);
  }

  function handleEditCancel() {
    setEditingUserId(null);
    setEditingUsername('');
    setEditingRole('observer');
    setEditingPassword('');
  }

  async function handleUpdate() {
    setIsSubmitting(true);
    setSuccessMessage(null);
    setErrorMessage(null);
    try {
      const body: UserUpdate = { role: editingRole };
      if (editingPassword.trim()) {
        body.password = editingPassword.trim();
      }
      await updateUser(editingUserId!, body);
      setEditingUserId(null);
      setEditingUsername('');
      setEditingRole('observer');
      setEditingPassword('');
      await refetch();
      setSuccessMessage('User updated successfully');
    } catch (err) {
      setErrorMessage(extractMessage(err, 'Operation failed'));
    } finally {
      setIsSubmitting(false);
    }
  }

  async function handleDelete(user: User) {
    setIsSubmitting(true);
    setSuccessMessage(null);
    setErrorMessage(null);
    try {
      await deleteUser(user.id);
      await refetch();
      setSuccessMessage('User deleted successfully');
    } catch (err) {
      setErrorMessage(extractMessage(err, 'Delete failed'));
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <div>
      <PageHeader
        title="User Management"
        actions={
          <button
            onClick={() => refetch()}
            disabled={isLoading || isFetching}
            className="px-3 py-1.5 text-sm bg-white border border-gray-300 rounded-md hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isFetching ? 'Refreshing…' : 'Refresh'}
          </button>
        }
      />
      <p className="text-sm text-gray-500 mb-6">Manage platform users and permissions</p>

      {/* Create form — operator and above */}
      <RequireRole roles={['operator', 'admin', 'super-admin']}>
        <form onSubmit={handleCreate} className="flex flex-wrap gap-2 mb-6 items-center">
          <input
            type="text"
            placeholder="Username"
            value={newUsername}
            onChange={(e) => setNewUsername(e.target.value)}
            disabled={isSubmitting}
            required
            className="border border-gray-300 rounded-md px-3 py-1.5 text-sm w-36 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
          />
          <input
            type="password"
            placeholder="Password"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            disabled={isSubmitting}
            required
            className="border border-gray-300 rounded-md px-3 py-1.5 text-sm w-36 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
          />
          <select
            value={newRole}
            onChange={(e) => setNewRole(e.target.value as Role)}
            disabled={isSubmitting}
            className="border border-gray-300 rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
          >
            {ROLES.map((r) => (
              <option key={r} value={r}>{ROLE_LABELS[r]}</option>
            ))}
          </select>
          <button
            type="submit"
            disabled={isSubmitting || !newUsername.trim() || !newPassword.trim()}
            className="px-3 py-1.5 text-sm bg-blue-600 text-white rounded-md hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isSubmitting ? 'Working…' : 'Create User'}
          </button>
        </form>
      </RequireRole>

      {successMessage && (
        <div className="mb-4 text-sm text-green-700">&#10003; {successMessage}</div>
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
            className="mt-3 px-3 py-1.5 text-sm bg-white border border-gray-300 rounded-md hover:bg-gray-50"
          >
            Retry
          </button>
        </div>
      ) : users.length === 0 ? (
        <p className="py-12 text-center text-gray-400 text-sm">No users found.</p>
      ) : (
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b border-gray-200 bg-gray-50">
              <th className="text-left px-4 py-2 font-medium text-gray-700">Username</th>
              <th className="text-left px-4 py-2 font-medium text-gray-700">Role</th>
              <th className="text-left px-4 py-2 font-medium text-gray-700">Actions</th>
            </tr>
          </thead>
          <tbody>
            {users.map((user) => {
              const isEditing = editingUserId === user.id;
              return (
                <tr key={user.id} className="border-b border-gray-100 hover:bg-gray-50">
                  <td className="px-4 py-2 text-gray-900">
                    {isEditing ? (
                      <input
                        type="text"
                        value={editingUsername}
                        disabled
                        className="border border-gray-200 rounded-md px-2 py-1 text-sm w-40 bg-gray-50 text-gray-500 cursor-not-allowed"
                      />
                    ) : (
                      <span className="font-mono text-xs">{user.username}</span>
                    )}
                  </td>
                  <td className="px-4 py-2 text-gray-900">
                    {isEditing ? (
                      <select
                        value={editingRole}
                        onChange={(e) => setEditingRole(e.target.value as Role)}
                        disabled={isSubmitting}
                        className="border border-gray-300 rounded-md px-2 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
                      >
                        {ROLES.map((r) => (
                          <option key={r} value={r}>{ROLE_LABELS[r]}</option>
                        ))}
                      </select>
                    ) : (
                      ROLE_LABELS[user.role as Role] ?? user.role
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
                          className="border border-gray-300 rounded-md px-2 py-1 text-sm w-36 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
                        />
                        <button
                          onClick={handleUpdate}
                          disabled={isSubmitting}
                          className="px-2 py-1 text-xs text-white bg-blue-600 border border-blue-600 rounded hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                          Save
                        </button>
                        <button
                          onClick={handleEditCancel}
                          disabled={isSubmitting}
                          className="px-2 py-1 text-xs text-gray-600 border border-gray-300 rounded hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                          Cancel
                        </button>
                      </div>
                    ) : (
                      <div className="flex gap-2">
                        <RequireRole roles={['operator', 'admin', 'super-admin']}>
                          <button
                            onClick={() => handleEditStart(user)}
                            disabled={isSubmitting || editingUserId !== null}
                            className="px-2 py-1 text-xs text-blue-600 border border-blue-300 rounded hover:bg-blue-50 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            Edit
                          </button>
                        </RequireRole>
                        <RequireRole roles={['admin', 'super-admin']}>
                          <button
                            onClick={() => handleDelete(user)}
                            disabled={isSubmitting}
                            className="px-2 py-1 text-xs text-red-600 border border-red-300 rounded hover:bg-red-50 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            Delete
                          </button>
                        </RequireRole>
                      </div>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
