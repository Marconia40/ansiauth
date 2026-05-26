'use client';

import { useState, useEffect, useRef } from 'react';
import { useAuth } from '@/context/AuthContext';
import { useJobNotifications } from '@/context/JobNotificationContext';
import { useQuery } from '@tanstack/react-query';
import { PageHeader } from '@/components/PageHeader';
import { LoadingSpinner } from '@/components/LoadingSpinner';
import { ErrorMessage } from '@/components/ErrorMessage';
import { getDevices, getVlans, createVlan, updateVlan, deleteVlan } from '@/services/api';
import type { Device } from '@/types/device';
import type { VlanEntry } from '@/types/vlan';

function extractMessage(error: unknown, fallback: string): string {
  const e = error as { response?: { data?: { detail?: string; message?: string } }; message?: string } | null;
  return e?.response?.data?.detail ?? e?.response?.data?.message ?? e?.message ?? fallback;
}

export default function VlansPage() {
  const { user } = useAuth();
  const { trackJob, trackGroupJob } = useJobNotifications();
  const canMutate = !!user && user.role !== 'observer';

  const [selectedDevices, setSelectedDevices] = useState<string[]>([]);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [deletingVlanId, setDeletingVlanId] = useState<number | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [newVlanId, setNewVlanId] = useState('');
  const [newVlanName, setNewVlanName] = useState('');
  const [editingVlanId, setEditingVlanId] = useState<number | null>(null);
  const [editingName, setEditingName] = useState('');

  const msgTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (msgTimerRef.current !== null) {
      clearTimeout(msgTimerRef.current);
      msgTimerRef.current = null;
    }
    if (!errorMessage) return;
    msgTimerRef.current = setTimeout(() => {
      setErrorMessage(null);
      msgTimerRef.current = null;
    }, 4000);
    return () => {
      if (msgTimerRef.current !== null) clearTimeout(msgTimerRef.current);
    };
  }, [errorMessage]);

  const {
    data: devices,
    isLoading: devicesLoading,
    error: devicesError,
  } = useQuery<Device[]>({
    queryKey: ['devices'],
    queryFn: getDevices,
  });

  const effectiveDevices =
    selectedDevices.length > 0
      ? selectedDevices
      : devices?.[0]?.name ? [devices[0].name] : [];

  const {
    data: vlans,
    isLoading: vlansLoading,
    isFetching: vlansFetching,
    error: vlansError,
    refetch,
  } = useQuery<VlanEntry[]>({
    queryKey: ['vlans', effectiveDevices[0]],
    queryFn: () => getVlans(effectiveDevices[0]),
    enabled: effectiveDevices.length > 0,
  });

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!newVlanId) { setErrorMessage('VLAN ID is required'); return; }
    if (!newVlanName.trim()) { setErrorMessage('VLAN name is required'); return; }
    setIsSubmitting(true);
    setErrorMessage(null);
    const capturedVlanId = newVlanId;
    try {
      const result = await createVlan({ vlan_id: Number(newVlanId), name: newVlanName.trim(), devices: effectiveDevices });
      setNewVlanId('');
      setNewVlanName('');
      await refetch();
      if (effectiveDevices.length > 1) {
        trackGroupJob(result.group_job_id, `Create VLAN ${capturedVlanId}`);
      } else {
        for (const j of result.jobs) {
          trackJob(j.job_id, `Create VLAN ${capturedVlanId}`, j.device);
        }
      }
    } catch (err) {
      setErrorMessage(extractMessage(err, 'Create failed'));
    } finally {
      setIsSubmitting(false);
    }
  }

  function handleEditStart(vlan: VlanEntry) {
    setEditingVlanId(vlan.vlan_id);
    setEditingName(vlan.name);
    setErrorMessage(null);
  }

  function handleEditCancel() {
    setEditingVlanId(null);
    setEditingName('');
  }

  async function handleUpdate() {
    if (!editingName.trim()) { setErrorMessage('VLAN name is required'); return; }
    setIsSubmitting(true);
    setErrorMessage(null);
    const capturedVlanId = editingVlanId!;
    try {
      const result = await updateVlan(editingVlanId!, { description: editingName.trim(), devices: effectiveDevices });
      setEditingVlanId(null);
      setEditingName('');
      await refetch();
      if (effectiveDevices.length > 1) {
        trackGroupJob(result.group_job_id, `Update VLAN ${capturedVlanId}`);
      } else {
        for (const j of result.jobs) {
          trackJob(j.job_id, `Update VLAN ${capturedVlanId}`, j.device);
        }
      }
    } catch (err) {
      setErrorMessage(extractMessage(err, 'Operation failed'));
    } finally {
      setIsSubmitting(false);
    }
  }

  async function handleDelete(vlan: VlanEntry) {
    const deviceList = effectiveDevices.join(', ');
    if (!window.confirm(`Delete VLAN ${vlan.vlan_id} from ${deviceList}?`)) return;
    setDeletingVlanId(vlan.vlan_id);
    setIsSubmitting(true);
    setErrorMessage(null);
    try {
      const result = await deleteVlan(vlan.vlan_id, { devices: effectiveDevices });
      await refetch();
      if (effectiveDevices.length > 1) {
        trackGroupJob(result.group_job_id, `Delete VLAN ${vlan.vlan_id}`);
      } else {
        for (const j of result.jobs) {
          trackJob(j.job_id, `Delete VLAN ${vlan.vlan_id}`, j.device);
        }
      }
    } catch (err) {
      setErrorMessage(extractMessage(err, 'Delete failed'));
    } finally {
      setIsSubmitting(false);
      setDeletingVlanId(null);
    }
  }

  return (
    <div>
      <PageHeader
        title="VLAN Management"
        actions={
          <button
            onClick={() => refetch()}
            disabled={vlansLoading || vlansFetching || effectiveDevices.length === 0 || isSubmitting}
            className="px-3 py-1.5 text-sm bg-white border border-gray-300 rounded-md hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {vlansFetching ? 'Refreshing...' : 'Refresh'}
          </button>
        }
      />
      <p className="text-sm text-gray-500 mb-6">View VLANs configured on managed devices</p>

      <div className="mb-6">
        {devicesLoading ? (
          <LoadingSpinner size="sm" />
        ) : devicesError ? (
          <ErrorMessage error={extractMessage(devicesError, 'Could not load devices')} />
        ) : devices && devices.length > 0 ? (
          <div className="flex flex-wrap gap-3">
            {devices.map((device) => (
              <label
                key={device.name}
                className="flex items-center gap-1.5 text-sm cursor-pointer select-none"
              >
                <input
                  type="checkbox"
                  checked={effectiveDevices.includes(device.name)}
                  onChange={(e) => {
                    setSelectedDevices((prev) =>
                      e.target.checked
                        ? [...prev, device.name]
                        : prev.filter((d) => d !== device.name),
                    );
                  }}
                  className="rounded border-gray-300 text-blue-600 focus:ring-blue-500"
                />
                <span className="text-gray-700">{device.name}</span>
              </label>
            ))}
          </div>
        ) : (
          <p className="text-sm text-gray-400">No devices available.</p>
        )}
      </div>

      {effectiveDevices.length > 0 && (
        <>
          {canMutate && (
            <form onSubmit={handleCreate} className="flex gap-2 mb-6 items-center">
              <input
                type="number"
                placeholder="VLAN ID"
                value={newVlanId}
                onChange={(e) => setNewVlanId(e.target.value)}
                disabled={isSubmitting}
                required
                className="border border-gray-300 rounded-md px-3 py-1.5 text-sm w-28 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
              />
              <input
                type="text"
                placeholder="VLAN Name"
                value={newVlanName}
                onChange={(e) => setNewVlanName(e.target.value)}
                disabled={isSubmitting}
                required
                className="border border-gray-300 rounded-md px-3 py-1.5 text-sm w-48 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
              />
              <button
                type="submit"
                disabled={isSubmitting || !newVlanId || !newVlanName}
                className="px-3 py-1.5 text-sm bg-blue-600 text-white rounded-md hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {isSubmitting && deletingVlanId === null && editingVlanId === null ? 'Creating...' : 'Create'}
              </button>
            </form>
          )}

          {errorMessage && (
            <div className="mb-4">
              <ErrorMessage error={`✗ ${errorMessage}`} />
            </div>
          )}

          {vlansLoading ? (
            <div className="py-12">
              <LoadingSpinner size="lg" />
            </div>
          ) : vlansError ? (
            <div className="py-6">
              <ErrorMessage error={extractMessage(vlansError, 'Could not load VLANs')} />
              <button
                onClick={() => refetch()}
                className="mt-3 px-3 py-1.5 text-sm bg-white border border-gray-300 rounded-md hover:bg-gray-50"
              >
                Retry
              </button>
            </div>
          ) : (
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="border-b border-gray-200 bg-gray-50">
                  <th className="text-left px-4 py-2 font-medium text-gray-700">VLAN ID</th>
                  <th className="text-left px-4 py-2 font-medium text-gray-700">Name</th>
                  <th className="text-left px-4 py-2 font-medium text-gray-700">Actions</th>
                </tr>
              </thead>
              <tbody>
                {vlans && vlans.length > 0 ? (
                  vlans.map((vlan) => {
                    const isEditing = editingVlanId === vlan.vlan_id;
                    return (
                      <tr key={vlan.vlan_id} className="border-b border-gray-100 hover:bg-gray-50">
                        <td className="px-4 py-2 text-gray-900">{vlan.vlan_id}</td>
                        <td className="px-4 py-2 text-gray-900">
                          {isEditing ? (
                            <input
                              type="text"
                              value={editingName}
                              onChange={(e) => setEditingName(e.target.value)}
                              disabled={isSubmitting}
                              autoFocus
                              className="border border-gray-300 rounded-md px-2 py-1 text-sm w-48 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
                            />
                          ) : (
                            vlan.name
                          )}
                        </td>
                        <td className="px-4 py-2 flex gap-2 items-center">
                          {canMutate && (isEditing ? (
                            <>
                              <button
                                onClick={handleUpdate}
                                disabled={isSubmitting || !editingName.trim()}
                                className="px-2 py-1 text-xs text-white bg-blue-600 border border-blue-600 rounded hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
                              >
                                {isSubmitting && deletingVlanId === null ? 'Saving...' : 'Save'}
                              </button>
                              <button
                                onClick={handleEditCancel}
                                disabled={isSubmitting}
                                className="px-2 py-1 text-xs text-gray-600 border border-gray-300 rounded hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
                              >
                                Cancel
                              </button>
                            </>
                          ) : (
                            <button
                              onClick={() => handleEditStart(vlan)}
                              disabled={isSubmitting || editingVlanId !== null}
                              className="px-2 py-1 text-xs text-blue-600 border border-blue-300 rounded hover:bg-blue-50 disabled:opacity-50 disabled:cursor-not-allowed"
                            >
                              Edit
                            </button>
                          ))}
                          {canMutate && (
                            <button
                              onClick={() => handleDelete(vlan)}
                              disabled={isSubmitting}
                              className="px-2 py-1 text-xs text-red-600 border border-red-300 rounded hover:bg-red-50 disabled:opacity-50 disabled:cursor-not-allowed"
                            >
                              {deletingVlanId === vlan.vlan_id ? 'Deleting...' : 'Delete'}
                            </button>
                          )}
                        </td>
                      </tr>
                    );
                  })
                ) : (
                  <tr>
                    <td colSpan={3} className="px-4 py-8 text-center text-gray-400">
                      No VLANs found for the selected device.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          )}
        </>
      )}
    </div>
  );
}
