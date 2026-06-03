// Helpers and constants shared across the Ports page and its components.
// Pure functions only — anything stateful lives in the page or in a hook.

export const DASH = '—';
export const PAGE_SIZE_OPTIONS = [25, 50, 100] as const;

export const SELECT_CLS =
  'px-2 py-1.5 text-sm border border-gray-300 rounded-md bg-white text-gray-700 focus:outline-none focus:ring-1 focus:ring-blue-400';

// Cisco-legacy reserved IDs that any switch will reject — fail fast in the
// browser instead of waiting for the backend to bounce the playbook.
export const RESERVED_VLANS = new Set([1002, 1003, 1004, 1005]);


export function extractMessage(error: unknown, fallback: string): string {
  const e = error as { response?: { data?: { detail?: string; message?: string } }; message?: string } | null;
  return e?.response?.data?.detail ?? e?.response?.data?.message ?? e?.message ?? fallback;
}

export function getStatus(error: unknown): number | null {
  const e = error as { response?: { status?: number } } | null;
  return e?.response?.status ?? null;
}

export function getErrorCode(error: unknown): string | null {
  const e = error as { response?: { data?: { error_code?: string } } } | null;
  return e?.response?.data?.error_code ?? null;
}

export function isUnsupportedVendorError(error: unknown): boolean {
  return getStatus(error) === 501 || getErrorCode(error) === 'VENDOR_NOT_SUPPORTED';
}


export function formatVlanList(vlans: number[] | null): string {
  if (vlans == null || !Array.isArray(vlans)) return DASH;
  if (vlans.length === 0) return 'None';
  if (vlans.length === 1) return String(vlans[0]);

  // Collapse consecutive ranges (e.g. [10,11,12,20,21] → "10-12, 20-21")
  const sorted = [...vlans].sort((a, b) => a - b);
  const ranges: string[] = [];
  let start = sorted[0];
  let prev = sorted[0];
  for (let i = 1; i < sorted.length; i++) {
    const v = sorted[i];
    if (v === prev + 1) {
      prev = v;
      continue;
    }
    ranges.push(start === prev ? `${start}` : `${start}-${prev}`);
    start = v;
    prev = v;
  }
  ranges.push(start === prev ? `${start}` : `${start}-${prev}`);

  const text = ranges.join(', ');
  // For huge trunk lists (e.g. 2-4094) collapse to one range + count to
  // avoid filling the table cell with thousands of comma-separated IDs.
  if (vlans.length > 16 && ranges.length === 1) {
    return `${ranges[0]} (${vlans.length})`;
  }
  return text;
}


// Parses user input like "10,20,30-35" or "10 20 30 to 35" into a sorted
// unique number[] and returns either the list or an error string.
export function parseVlanInput(raw: string): { vlans: number[]; error: string | null } {
  const tokens = raw.split(/[\s,]+/).filter(Boolean);
  if (tokens.length === 0) return { vlans: [], error: 'Enter at least one VLAN ID' };

  const set = new Set<number>();
  for (const tok of tokens) {
    const range = tok.match(/^(\d+)(?:-|to)(\d+)$/i);
    if (range) {
      const lo = parseInt(range[1], 10);
      const hi = parseInt(range[2], 10);
      if (lo > hi) return { vlans: [], error: `Invalid range: ${tok} (start must be ≤ end)` };
      for (let v = lo; v <= hi; v++) set.add(v);
      continue;
    }
    const n = parseInt(tok, 10);
    if (isNaN(n) || String(n) !== tok.trim()) {
      return { vlans: [], error: `'${tok}' is not a valid VLAN ID` };
    }
    set.add(n);
  }

  const vlans = [...set].sort((a, b) => a - b);

  for (const v of vlans) {
    if (v < 1 || v > 4094) return { vlans: [], error: `VLAN ${v} is out of range (1–4094)` };
    if (RESERVED_VLANS.has(v)) return { vlans: [], error: `VLAN ${v} is reserved (Cisco legacy)` };
  }

  return { vlans, error: null };
}
