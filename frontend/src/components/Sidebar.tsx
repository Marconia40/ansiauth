'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useHasRole } from '@/components/RequireRole';
import type { Role } from '@/types/auth';

const NAV_ITEMS: { href: string; label: string; minRole: Role }[] = [
  { href: '/', label: 'Dashboard', minRole: 'observer' },
  { href: '/vlans', label: 'VLANs', minRole: 'observer' },
  { href: '/devices', label: 'Devices', minRole: 'observer' },
  { href: '/device-groups', label: 'Device Groups', minRole: 'observer' },
  { href: '/jobs', label: 'Jobs', minRole: 'observer' },
  { href: '/users', label: 'Users', minRole: 'admin' },
  { href: '/audit', label: 'Audit Log', minRole: 'super-admin' },
];

export function Sidebar() {
  const pathname = usePathname();

  return (
    <aside className="w-64 min-h-screen bg-gray-900 text-white flex flex-col shrink-0">
      <div className="px-6 py-5 border-b border-gray-700">
        <span className="text-lg font-bold tracking-wide">AnsiAuth</span>
      </div>
      <nav className="flex-1 px-4 py-4 space-y-1">
        {NAV_ITEMS.map((item) => (
          <NavItem
            key={item.href}
            href={item.href}
            label={item.label}
            minRole={item.minRole}
            active={pathname === item.href}
          />
        ))}
      </nav>
    </aside>
  );
}

function NavItem({
  href,
  label,
  minRole,
  active,
}: {
  href: string;
  label: string;
  minRole: Role;
  active: boolean;
}) {
  const allowed = useHasRole(minRole);
  if (!allowed) return null;

  return (
    <Link
      href={href}
      className={`block px-3 py-2 rounded-md text-sm font-medium transition-colors ${
        active
          ? 'bg-gray-700 text-white'
          : 'text-gray-300 hover:bg-gray-700 hover:text-white'
      }`}
    >
      {label}
    </Link>
  );
}
