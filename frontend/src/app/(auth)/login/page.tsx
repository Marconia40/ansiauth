'use client';

import { useState, useEffect, type FormEvent } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/context/AuthContext';
import { consumeSessionExpiredFlag, login } from '@/services/api';
import { Brand } from '@/components/Brand';

export default function LoginPage() {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [loading, setLoading] = useState(false);
  const { user, setUser, isInitializing } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (consumeSessionExpiredFlag()) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setNotice('Session expired. Please sign in again.');
    }
  }, []);

  useEffect(() => {
    if (!isInitializing && user) router.push('/');
  }, [user, isInitializing, router]);

  if (isInitializing) return null;

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      const user = await login(username, password);
      setUser(user);
      router.push('/');
    } catch {
      setError('Invalid username or password');
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="login-shell relative min-h-screen flex items-center justify-center overflow-hidden">
      <div className="relative z-10 w-full max-w-sm mx-4">
        <div className="rounded-2xl bg-white/95 backdrop-blur-sm shadow-2xl border border-white/60 px-7 py-6">
          <div className="flex flex-col items-center mb-6">
            <Brand variant="wordmark" tone="color" className="h-28 w-auto" />
            <p className="mt-1 text-xs text-gray-500 tracking-wide">
              Network Automation Platform
            </p>
          </div>

          {notice && (
            <div className="mb-4 text-sm text-amber-800 bg-amber-50 border border-amber-200 rounded-md px-3 py-2">
              {notice}
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-xs font-semibold uppercase tracking-wider text-gray-600 mb-1.5">
                Username
              </label>
              <input
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                className="w-full px-3 py-2.5 bg-white border border-gray-300 rounded-md text-sm text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-[#2f4b7c] focus:border-[#2f4b7c] transition"
                autoComplete="username"
                autoFocus
                required
              />
            </div>

            <div>
              <label className="block text-xs font-semibold uppercase tracking-wider text-gray-600 mb-1.5">
                Password
              </label>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="w-full px-3 py-2.5 bg-white border border-gray-300 rounded-md text-sm text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-[#2f4b7c] focus:border-[#2f4b7c] transition"
                autoComplete="current-password"
                required
              />
            </div>

            {error && (
              <p className="text-sm text-red-600" role="alert">
                {error}
              </p>
            )}

            <button
              type="submit"
              disabled={loading}
              className="w-full py-2.5 px-4 bg-[#2f4b7c] text-white text-sm font-semibold rounded-md hover:bg-[#26406b] disabled:opacity-60 disabled:cursor-not-allowed transition-colors shadow-sm"
            >
              {loading ? 'Signing in…' : 'Sign In'}
            </button>
          </form>
        </div>

        <p className="mt-6 text-center text-xs text-white/60">
          © {new Date().getFullYear()} AnsiAuth · Secure network orchestration
        </p>
      </div>
    </div>
  );
}

