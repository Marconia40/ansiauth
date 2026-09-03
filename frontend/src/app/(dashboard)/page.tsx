import { ScopeShell } from '@/components/scope/ScopeShell';
import type { ScopeTab } from '@/components/scope/ScopeTabs';
import { PlaceholderPanel } from '@/components/scope/Panel';

// At the Organization scope the VLAN/PORTS/VI/GC actions target ALL devices
// under every site the caller can see. Enabling them here is a later block —
// for now only the dashboard tab is active. The strip stays visible so users
// can see the shared navigation model.
const ORG_TABS: ScopeTab[] = [
  { label: 'Dashboard', segment: '' },
  { label: 'VLAN', segment: 'vlan', disabled: true, disabledReason: 'Enable per-site or per-group' },
  { label: 'PORTS', segment: 'ports', disabled: true, disabledReason: 'Enable per-site or per-group' },
  {
    label: 'VIRTUAL-INTERFACES',
    segment: 'virtual-interfaces',
    disabled: true,
    disabledReason: 'Coming soon — backend endpoint pending',
  },
  {
    label: 'GLOBAL_CONFIG',
    segment: 'global-config',
    disabled: true,
    disabledReason: 'Coming soon — backend endpoint pending',
  },
];

export default function OrgDashboardPage() {
  return (
    <ScopeShell
      crumbs={[{ label: 'Management', href: '/' }]}
      tabsBase="/"
      tabs={ORG_TABS}
    >
      <PlaceholderPanel label="Organization dashboard — 4 cards + jobs pie (block 2)" />
    </ScopeShell>
  );
}
