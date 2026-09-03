'use client';

import Link from 'next/link';
import { useEffect, useRef, useState } from 'react';
import { usePathname, useRouter } from 'next/navigation';
import { useAuth } from '@/context/AuthContext';
import { logout } from '@/services/api';
import {
  AuditIcon,
  InventoryIcon,
  KeyIcon,
  LogoutIcon,
  UserCircleIcon,
  UsersIcon,
} from './Icon';

const TOP_LINKS: {
  href: string;
  label: string;
  icon: (props: { size?: number }) => React.ReactElement;
  adminOnly?: boolean;
}[] = [
  { href: '/users', label: 'Users', icon: UsersIcon, adminOnly: true },
  { href: '/audit', label: 'Audit', icon: AuditIcon },
  { href: '/inventory', label: 'Inventory', icon: InventoryIcon },
];

export function Topbar() {
  const { user } = useAuth();
  const pathname = usePathname();

  return (
    <header className="h-14 bg-header text-header-fg flex items-center justify-between px-4 shrink-0 shadow">
      <Link href="/" className="flex items-center gap-3">
        <div className="h-9 w-24 rounded-md bg-white/95 text-inverse flex items-center justify-center text-sm font-bold tracking-widest">
          LOGO
        </div>
      </Link>

      <div className="flex items-center gap-1">
        {TOP_LINKS.map((link) => {
          if (link.adminOnly && !user?.is_system_admin) return null;
          const active = pathname === link.href || pathname.startsWith(`${link.href}/`);
          return (
            <TopIconLink
              key={link.href}
              href={link.href}
              label={link.label}
              icon={<link.icon size={22} />}
              active={active}
            />
          );
        })}
        <SessionMenu />
      </div>
    </header>
  );
}

function TopIconLink({
  href,
  label,
  icon,
  active,
}: {
  href: string;
  label: string;
  icon: React.ReactNode;
  active: boolean;
}) {
  return (
    <Link
      href={href}
      title={label}
      aria-label={label}
      className={`h-10 w-10 flex items-center justify-center rounded-md transition-colors ${
        active ? 'bg-white/20' : 'hover:bg-white/10'
      }`}
    >
      {icon}
    </Link>
  );
}

function SessionMenu() {
  const { user, setUser } = useAuth();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    function handleKey(e: KeyboardEvent) {
      if (e.key === 'Escape') setOpen(false);
    }
    document.addEventListener('mousedown', handleClick);
    document.addEventListener('keydown', handleKey);
    return () => {
      document.removeEventListener('mousedown', handleClick);
      document.removeEventListener('keydown', handleKey);
    };
  }, [open]);

  async function handleLogout() {
    await logout();
    setUser(null);
    router.push('/login');
  }

  return (
    <div className="relative ml-1" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-label="Session menu"
        className={`h-10 w-10 flex items-center justify-center rounded-md transition-colors ${
          open ? 'bg-white/20' : 'hover:bg-white/10'
        }`}
      >
        <UserCircleIcon size={22} />
      </button>

      {open && (
        <div className="absolute right-0 mt-2 w-56 rounded-md bg-panel text-text border border-panel-border shadow-lg py-1 z-50">
          <div className="px-3 py-2 border-b border-panel-border">
            <p className="text-sm font-semibold truncate">{user?.username ?? '—'}</p>
            <p className="text-xs text-muted">
              {user?.is_system_admin ? 'System administrator' : 'Standard user'}
            </p>
          </div>
          <button
            type="button"
            onClick={() => {
              setOpen(false);
              router.push('/session/password');
            }}
            className="w-full flex items-center gap-2 px-3 py-2 text-sm hover:bg-panel-elev"
          >
            <KeyIcon size={16} />
            Change password
          </button>
          <button
            type="button"
            onClick={handleLogout}
            className="w-full flex items-center gap-2 px-3 py-2 text-sm hover:bg-panel-elev text-danger"
          >
            <LogoutIcon size={16} />
            Sign out
          </button>
        </div>
      )}
    </div>
  );
}
