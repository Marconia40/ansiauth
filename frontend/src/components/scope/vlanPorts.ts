import type { Port } from '@/types/port';

/**
 * Returns the port numbers (last numeric segment of the interface name) where
 * the given VLAN is configured on a single device. Ports where the VLAN is
 * untagged / native carry a trailing `(U)`.
 *
 * Semantics on top of the backend Port schema:
 *   - Access ports with `access_vlan === X` → untagged X.
 *   - Trunk ports with `access_vlan === X` (PVID / native) → untagged X.
 *   - Trunk ports with `allowed_vlans` containing X → tagged X.
 */
export function vlanPortsForDevice(
  ports: Port[],
  vlanId: number,
): string[] {
  const out: { key: string; label: string; sortKey: number }[] = [];
  for (const p of ports) {
    const num = extractPortNumber(p.name);
    if (num === null) continue;
    let label: string | null = null;
    if (p.mode === 'access') {
      if (p.access_vlan === vlanId) label = `${num}(U)`;
    } else if (p.mode === 'trunk') {
      if (p.access_vlan === vlanId) {
        label = `${num}(U)`;
      } else if (p.allowed_vlans?.includes(vlanId)) {
        label = String(num);
      }
    } else {
      // Unknown mode — best-effort match on access_vlan only.
      if (p.access_vlan === vlanId) label = `${num}(U)`;
    }
    if (label !== null) {
      out.push({ key: p.name, label, sortKey: num });
    }
  }
  out.sort((a, b) => a.sortKey - b.sortKey);
  return out.map((o) => o.label);
}

/** `GigabitEthernet0/0/14` → 14. `Ethernet1/0/3` → 3. Returns null when no
 * trailing number could be extracted. */
export function extractPortNumber(name: string): number | null {
  const m = name.match(/(\d+)\s*$/);
  return m ? Number(m[1]) : null;
}
