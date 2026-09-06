'use client';

import { useState } from 'react';
import { PlaceholderPanel } from './Panel';
import { GlobalConfigOverview } from './GlobalConfigOverview';
import { GlobalConfigRoutes } from './GlobalConfigRoutes';
import { GlobalConfigAcls } from './GlobalConfigAcls';
import { GlobalConfigArpMac } from './GlobalConfigArpMac';

interface Props {
  deviceName: string;
}

type Section = 'overview' | 'routes' | 'acls' | 'arp-mac' | 'logs' | 'running-config';

interface SectionDef {
  key: Section;
  label: string;
}

const SECTIONS: SectionDef[] = [
  { key: 'overview', label: 'Overview' },
  { key: 'routes', label: 'Routes' },
  { key: 'acls', label: 'ACLs' },
  { key: 'arp-mac', label: 'ARP / MAC' },
  { key: 'logs', label: 'Logs' },
  { key: 'running-config', label: 'Running-config' },
];

// Device-scoped Global Configuration tab (SRS §3.4, RF-GLOBAL-01..09).
// Overview groups the "individual settings" (hostname, SNMP, NTP, DNS,
// logging, version) that share one sync scope. The remaining sections each
// hit their own backend endpoint with distinct sync semantics -- ARP/MAC and
// logs, in particular, have separate sync scopes and are NOT populated on
// device registration, so they get their own dedicated refresh button.
export function GlobalConfigTab({ deviceName }: Props) {
  const [section, setSection] = useState<Section>('overview');

  return (
    <div className="flex flex-col gap-4">
      <SubTabStrip current={section} onSelect={setSection} />
      {section === 'overview' ? (
        <GlobalConfigOverview deviceName={deviceName} />
      ) : section === 'routes' ? (
        <GlobalConfigRoutes deviceName={deviceName} />
      ) : section === 'acls' ? (
        <GlobalConfigAcls deviceName={deviceName} />
      ) : section === 'arp-mac' ? (
        <GlobalConfigArpMac deviceName={deviceName} />
      ) : (
        <PlaceholderPanel
          label={`${SECTIONS.find((s) => s.key === section)?.label ?? section} — coming in the next block (${deviceName}).`}
        />
      )}
    </div>
  );
}

function SubTabStrip({
  current,
  onSelect,
}: {
  current: Section;
  onSelect: (s: Section) => void;
}) {
  return (
    <div className="border-b border-panel-border flex items-end gap-1 -mb-px overflow-x-auto">
      {SECTIONS.map((s) => {
        const active = s.key === current;
        return (
          <button
            type="button"
            key={s.key}
            onClick={() => onSelect(s.key)}
            className={`px-4 py-2 text-xs font-semibold uppercase tracking-wider transition-colors border-b-2 ${
              active
                ? 'border-info text-text bg-panel/60'
                : 'border-transparent text-muted hover:text-text hover:bg-panel/40'
            }`}
          >
            {s.label}
          </button>
        );
      })}
    </div>
  );
}
