'use client';

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { PageHeader } from '@/components/PageHeader';
import { LoadingSpinner } from '@/components/LoadingSpinner';
import { ErrorMessage } from '@/components/ErrorMessage';
import { getDevices, getVlans, createVlan, deleteVlan } from '@/services/api';
import type { Device } from '@/types/device';
import type { VlanEntry } from '@/types/vlan';

function extractMessage(error: unknown, fallback: string): string {
  const e = error as { response?: { data?: { detail?: string } }; message?: string } | null;
  return e?.response?.data?.detail ?? e?.message ?? fallback;
}

export default function VlansPage() {
  const [selectedDevice, setSelectedDevice] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [actionError, setActionError] = useState('');
  const [newVlanId, setNewVlanId] = useState('');
  const [newVlanName, setNewVlanName] = useState('');

  const {
    data: devices,
    isLoading: devicesLoading,
    error: devicesError,
  } = useQuery<Device[]>({
    queryKey: ['devices'],
    queryFn: getDevices,
  });

  const effectiveDevice = selectedDevice || (devices?.[0]?.name ?? '');

  const {
    data: vlans,
    isLoading: vlansLoading,
    error: vlansError,
    refetch,
  } = useQuery<VlanEntry[]>({
    queryKey: ['vlans', effectiveDevice],
    queryFn: () => getVlans(effectiveDevice),
    enabled: !!effectiveDevice,
  });

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!newVlanId || !newVlanName) return;
    setIsSubmitting(true);
    setActionError('');
    try {
      await createVlan({ vlan_id: Number(newVlanId), name: newVlanName, devices: [effectiveDevice] });
      setNewVlanId('');
      setNewVlanName('');
      await refetch();
    } catch (err) {
      setActionError(extractMessage(err, 'Create failed'));
    } finally {
      setIsSubmitting(false);
    }
  }

  async function handleDelete(vlan: VlanEntry) {
    if (!window.confirm(`Delete VLAN ${vlan.vlan_id} from ${effectiveDevice}?`)) return;
    setIsSubmitting(true);
    setActionError('');
    try {
      await deleteVlan(vlan.vlan_id, { devices: [effectiveDevice] });
      await refetch();
    } catch (err) {
      setActionError(extractMessage(err, 'Delete failed'));
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <div>
      <PageHeader
        title="VLAN Management"
        actions={
          <button
            onClick={() => refetch()}
            disabled={vlansLoading || !effectiveDevice || isSubmitting}
            className="px-3 py-1.5 text-sm bg-white border border-gray-300 rounded-md hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Refresh
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
          <select
            value={effectiveDevice}
            onChange={(e) => setSelectedDevice(e.target.value)}
            className="border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            {devices.map((device) => (
              <option key={device.name} value={device.name}>
                {device.name}
              </option>
            ))}
          </select>
        ) : (
          <p className="text-sm text-gray-400">No devices available.</p>
        )}
      </div>

      {effectiveDevice && (
        <>
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
              {isSubmitting ? 'Working…' : 'Create'}
            </button>
          </form>

          {actionError && (
            <div className="mb-4">
              <ErrorMessage error={actionError} />
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
                  vlans.map((vlan) => (
                    <tr key={vlan.vlan_id} className="border-b border-gray-100 hover:bg-gray-50">
                      <td className="px-4 py-2 text-gray-900">{vlan.vlan_id}</td>
                      <td className="px-4 py-2 text-gray-900">{vlan.name}</td>
                      <td className="px-4 py-2">
                        <button
                          onClick={() => handleDelete(vlan)}
                          disabled={isSubmitting}
                          className="px-2 py-1 text-xs text-red-600 border border-red-300 rounded hover:bg-red-50 disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                          Delete
                        </button>
                      </td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={3} className="px-4 py-8 text-center text-gray-400">
                      No VLANs found for this device.
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
