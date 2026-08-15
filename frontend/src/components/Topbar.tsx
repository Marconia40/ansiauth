'use client';

import { useRouter } from 'next/navigation';
import { useAuth } from '@/context/AuthContext';
import { logout } from '@/services/api';

const ROLE_COLORS: Record<string, string> = {
  observer: 'bg-gray-100 text-gray-700',
  operator: 'bg-blue-100 text-blue-700',
  admin: 'bg-purple-100 text-purple-700',
  'super-admin': 'bg-red-100 text-red-700',
};

export function Topbar() {
  const { user, setUser } = useAuth();
  const router = useRouter();

  async function handleLogout() {
    await logout();
    setUser(null);
    router.push('/login');
  }

  return (
    <header className="h-14 bg-white border-b border-gray-200 flex items-center justify-end px-6 shrink-0">
      {user && (
        <div className="flex items-center gap-4">
          <span className="text-sm text-gray-600">{user.username}</span>
          <span
            className={`text-xs px-2 py-1 rounded-full font-medium ${ROLE_COLORS[user.role] ?? 'bg-gray-100 text-gray-700'}`}
          >
            {user.role}
          </span>
          <button
            onClick={handleLogout}
            className="text-sm text-gray-500 hover:text-gray-900 transition-colors"
          >
            Logout
          </button>
        </div>
      )}
    </header>
  );
}
