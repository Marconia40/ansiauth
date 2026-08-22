'use client';

import { useState, useEffect, useRef } from 'react';
import Link from 'next/link';
import { useQuery } from '@tanstack/react-query';
import { PageHeader } from '@/components/PageHeader';
import { LoadingSpinner } from '@/components/LoadingSpinner';
import { ErrorMessage } from '@/components/ErrorMessage';
import { RequireRole } from '@/components/RequireRole';
import { getSites, createSite, updateSite, deleteSite } from '@/services/api';
import type { Site, SiteUpdate } from '@/types/site';

function extractMessage(error: unknown, fallback: string): string {
  const e = error as { response?: { data?: { detail?: string; message?: string } }; message?: string } | null;
  return e?.response?.data?.detail ?? e?.response?.data?.message ?? e?.message ?? fallback;
}

function normalizeSites(data: unknown): Site[] {
  if (Array.isArray(data)) return data as Site[];
  return [];
}

export default function SitesPage() {
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [deletingSiteId, setDeletingSiteId] = useState<number | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const [newName, setNewName] = useState('');
  const [newDescription, setNewDescription] = useState('');

  const [editingSiteId, setEditingSiteId] = useState<number | null>(null);
  const [editingName, setEditingName] = useState('');
  const [editingDescription, setEditingDescription] = useState('');

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
    data: sitesRaw,
    isLoading,
    error: sitesError,
    refetch,
    isFetching,
  } = useQuery({ queryKey: ['sites'], queryFn: getSites });

  const sites = normalizeSites(sitesRaw);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    const name = newName.trim();
    if (!name) { setErrorMessage('Name is required'); return; }
    setIsSubmitting(true);
    setSuccessMessage(null);
    setErrorMessage(null);
    try {
      await createSite({
        name,
        description: newDescription.trim() || undefined,
      });
      setNewName('');
      setNewDescription('');
      await refetch();
      setSuccessMessage(`Site ${name} created successfully`);
    } catch (err) {
      setErrorMessage(extractMessage(err, 'Create failed'));
    } finally {
      setIsSubmitting(false);
    }
  }

  function handleEditStart(site: Site) {
    setEditingSiteId(site.id);
    setEditingName(site.name);
    setEditingDescription(site.description ?? '');
    setSuccessMessage(null);
    setErrorMessage(null);
  }

  function handleEditCancel() {
    setEditingSiteId(null);
    setEditingName('');
    setEditingDescription('');
  }

  async function handleUpdate() {
    if (editingSiteId === null) return;
    const name = editingName.trim();
    if (!name) { setErrorMessage('Name is required'); return; }
    setIsSubmitting(true);
    setSuccessMessage(null);
    setErrorMessage(null);
    try {
      const body: SiteUpdate = { name, description: editingDescription.trim() };
      await updateSite(editingSiteId, body);
      const updatedName = name;
      setEditingSiteId(null);
      setEditingName('');
      setEditingDescription('');
      await refetch();
      setSuccessMessage(`Site ${updatedName} updated successfully`);
    } catch (err) {
      setErrorMessage(extractMessage(err, 'Update failed'));
    } finally {
      setIsSubmitting(false);
    }
  }

  async function handleDelete(site: Site) {
    if (!window.confirm(`Delete site ${site.name}?`)) return;
    setDeletingSiteId(site.id);
    setIsSubmitting(true);
    setSuccessMessage(null);
    setErrorMessage(null);
    try {
      await deleteSite(site.id);
      await refetch();
      setSuccessMessage(`Site ${site.name} deleted successfully`);
    } catch (err) {
      setErrorMessage(extractMessage(err, 'Delete failed'));
    } finally {
      setIsSubmitting(false);
      setDeletingSiteId(null);
    }
  }

  return (
    <div>
      <PageHeader
        title="Sites"
        actions={
          <button
            onClick={() => refetch()}
            disabled={isLoading || isFetching || isSubmitting}
            className="px-3 py-1.5 text-sm bg-white border border-gray-300 rounded-md hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isFetching ? 'Refreshing...' : 'Refresh'}
          </button>
        }
      />
      <p className="text-sm text-gray-500 mb-6">Organize infrastructure into physical or logical sites</p>

      <RequireRole roles={['admin', 'super-admin']}>
        <form onSubmit={handleCreate} className="flex flex-wrap gap-2 mb-6 items-center">
          <input
            type="text"
            placeholder="Name"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            disabled={isSubmitting}
            required
            className="border border-gray-300 rounded-md px-3 py-1.5 text-sm w-40 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
          />
          <input
            type="text"
            placeholder="Description (optional)"
            value={newDescription}
            onChange={(e) => setNewDescription(e.target.value)}
            disabled={isSubmitting}
            className="border border-gray-300 rounded-md px-3 py-1.5 text-sm w-64 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
          />
          <button
            type="submit"
            disabled={isSubmitting || !newName.trim()}
            className="px-3 py-1.5 text-sm bg-blue-600 text-white rounded-md hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isSubmitting && deletingSiteId === null && editingSiteId === null ? 'Creating...' : 'Create Site'}
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
      ) : sitesError ? (
        <div className="py-6">
          <ErrorMessage error={extractMessage(sitesError, 'Could not load sites')} />
          <button
            onClick={() => refetch()}
            className="mt-3 px-3 py-1.5 text-sm bg-white border border-gray-300 rounded-md hover:bg-gray-50"
          >
            Retry
          </button>
        </div>
      ) : sites.length === 0 ? (
        <p className="py-12 text-center text-gray-400 text-sm">No sites yet.</p>
      ) : (
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b border-gray-200 bg-gray-50">
              <th className="text-left px-4 py-2 font-medium text-gray-700">Name</th>
              <th className="text-left px-4 py-2 font-medium text-gray-700">Description</th>
              <th className="text-left px-4 py-2 font-medium text-gray-700">Devices</th>
              <th className="text-left px-4 py-2 font-medium text-gray-700">Default group</th>
              <th className="text-left px-4 py-2 font-medium text-gray-700">Actions</th>
            </tr>
          </thead>
          <tbody>
            {sites.map((site) => {
              const isEditing = editingSiteId === site.id;
              const isBaseInfra = site.kind === 'BASE_INFRASTRUCTURE';
              return (
                <tr key={site.id} className="border-b border-gray-100 hover:bg-gray-50">
                  <td className="px-4 py-2 text-gray-900">
                    {isEditing ? (
                      <input
                        type="text"
                        value={editingName}
                        onChange={(e) => setEditingName(e.target.value)}
                        disabled={isSubmitting || isBaseInfra}
                        className="border border-gray-300 rounded-md px-2 py-1 text-sm w-40 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
                      />
                    ) : (
                      <span className="flex items-center gap-2">
                        <Link
                          href={`/sites/${site.id}`}
                          className="font-medium text-blue-700 hover:underline"
                        >
                          {site.name}
                        </Link>
                        {isBaseInfra && (
                          <span
                            title="System-managed base site — visible to system-admins only (D14)"
                            className="text-[10px] px-2 py-0.5 bg-amber-100 text-amber-800 rounded-full uppercase tracking-wide"
                          >
                            Base Infra
                          </span>
                        )}
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-2 text-gray-700">
                    {isEditing ? (
                      <input
                        type="text"
                        value={editingDescription}
                        onChange={(e) => setEditingDescription(e.target.value)}
                        disabled={isSubmitting}
                        placeholder="Description"
                        className="border border-gray-300 rounded-md px-2 py-1 text-sm w-64 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
                      />
                    ) : (
                      <span className="text-gray-600">{site.description ?? '—'}</span>
                    )}
                  </td>
                  <td className="px-4 py-2 text-gray-700">{site.device_count}</td>
                  <td className="px-4 py-2 text-gray-700">
                    <span className="inline-block text-xs px-2 py-0.5 bg-gray-100 text-gray-700 rounded-full font-mono">
                      #{site.default_group_id}
                    </span>
                  </td>
                  <td className="px-4 py-2">
                    {isEditing ? (
                      <div className="flex flex-wrap gap-2 items-center">
                        <button
                          onClick={handleUpdate}
                          disabled={isSubmitting || !editingName.trim()}
                          className="px-2 py-1 text-xs text-white bg-blue-600 border border-blue-600 rounded hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                          {isSubmitting && deletingSiteId === null ? 'Saving...' : 'Save'}
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
                        <RequireRole roles={['admin', 'super-admin']}>
                          <button
                            onClick={() => handleEditStart(site)}
                            disabled={isSubmitting || editingSiteId !== null}
                            className="px-2 py-1 text-xs text-blue-600 border border-blue-300 rounded hover:bg-blue-50 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            Edit
                          </button>
                        </RequireRole>
                        <RequireRole roles={['admin', 'super-admin']}>
                          {isBaseInfra ? (
                            <span
                              className="text-xs text-gray-400 italic"
                              title="Base-Infrastructure is system-managed and cannot be deleted"
                            >
                              locked
                            </span>
                          ) : (
                            <button
                              onClick={() => handleDelete(site)}
                              disabled={isSubmitting}
                              className="px-2 py-1 text-xs text-red-600 border border-red-300 rounded hover:bg-red-50 disabled:opacity-50 disabled:cursor-not-allowed"
                            >
                              {deletingSiteId === site.id ? 'Deleting...' : 'Delete'}
                            </button>
                          )}
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
