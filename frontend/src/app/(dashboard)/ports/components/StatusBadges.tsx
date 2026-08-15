import type { PortMode } from '@/types/port';

export function RowSpinner() {
  return (
    <div className="inline-block w-3 h-3 animate-spin rounded-full border-2 border-gray-300 border-t-blue-500" />
  );
}

export function AdminBadge({ value }: { value: boolean | null }) {
  if (value === null) {
    return (
      <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-gray-100 text-gray-500">
        Unknown
      </span>
    );
  }
  if (value) {
    return (
      <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-green-100 text-green-700">
        Enabled
      </span>
    );
  }
  return (
    <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-gray-200 text-gray-600">
      Disabled
    </span>
  );
}

export function OperBadge({ value }: { value: boolean | null }) {
  if (value === null) {
    return (
      <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-gray-100 text-gray-500">
        Unknown
      </span>
    );
  }
  if (value) {
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium bg-green-100 text-green-700">
        <span className="w-1.5 h-1.5 rounded-full bg-green-500" />
        Up
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium bg-red-100 text-red-700">
      <span className="w-1.5 h-1.5 rounded-full bg-red-500" />
      Down
    </span>
  );
}

export function ModeBadge({ mode }: { mode: PortMode }) {
  const cls =
    mode === 'access'
      ? 'bg-blue-50 text-blue-700 ring-1 ring-blue-200'
      : mode === 'trunk'
      ? 'bg-purple-50 text-purple-700 ring-1 ring-purple-200'
      : 'bg-gray-100 text-gray-500 ring-1 ring-gray-200';
  const label = mode === 'unknown' ? 'Unknown' : mode.charAt(0).toUpperCase() + mode.slice(1);
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${cls}`}>
      {label}
    </span>
  );
}
