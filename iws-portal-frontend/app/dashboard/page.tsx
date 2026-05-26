'use client';
import { useEffect, useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';
const IDLE_TIMEOUT = 30 * 60 * 1000;

interface User {
  full_name: string;
  email: string;
  role: string;
  entity_id: string | number;
}

function formatRole(role: string): string {
  return role.charAt(0).toUpperCase() + role.slice(1).toLowerCase();
}

export default function DashboardPage() {
  const [user, setUser] = useState<User | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loggingOut, setLoggingOut] = useState(false);
  const router = useRouter();

  const handleLogout = useCallback(async () => {
    setLoggingOut(true);
    try {
      await fetch(`${API_URL}/api/v1/auth/logout`, {
        method: 'POST',
        credentials: 'include',
      });
    } catch {
      // best-effort logout; redirect regardless
    } finally {
      router.push('/');
    }
  }, [router]);

  useEffect(() => {
    let idleTimer: NodeJS.Timeout;
    const resetTimer = () => {
      clearTimeout(idleTimer);
      idleTimer = setTimeout(handleLogout, IDLE_TIMEOUT);
    };
    const events = ['mousedown', 'keypress', 'touchstart'] as const;
    events.forEach(e => window.addEventListener(e, resetTimer));
    window.addEventListener('scroll', resetTimer, { passive: true });
    resetTimer();
    return () => {
      clearTimeout(idleTimer);
      events.forEach(e => window.removeEventListener(e, resetTimer));
      window.removeEventListener('scroll', resetTimer);
    };
  }, [handleLogout]);

  useEffect(() => {
    const controller = new AbortController();
    fetch(`${API_URL}/api/v1/me`, {
      credentials: 'include',
      signal: controller.signal,
    })
      .then(res => {
        if (res.status === 401) {
          router.push('/');
          return null;
        }
        if (!res.ok) throw new Error('Unable to load your session. Please try again.');
        return res.json();
      })
      .then(data => {
        if (data) setUser(data);
      })
      .catch(err => {
        if (err.name === 'AbortError') return;
        setError(err.message);
        setTimeout(() => router.push('/'), 3000);
      });
    return () => controller.abort();
  }, [router]);

  if (error) return (
    <main id="main-content" className="min-h-screen flex items-center justify-center bg-page px-4 py-10">
      <div className="bg-card p-6 sm:p-8 rounded-lg shadow-sm border border-rule text-center w-full max-w-sm" role="alert">
        <p className="text-peril font-medium mb-2">Session error</p>
        <p className="text-dim text-sm">{error}</p>
        <p className="text-ghost text-xs mt-2">
          Redirecting to login, or{' '}
          <a href="/" className="text-prime underline-offset-2 hover:underline">go now</a>.
        </p>
      </div>
    </main>
  );

  if (!user) return (
    <main id="main-content" className="min-h-screen bg-page p-4 sm:p-8" aria-busy="true">
      <span role="status" aria-live="polite" className="sr-only">Loading dashboard</span>
      <div className="max-w-4xl mx-auto">
        <div className="flex flex-wrap justify-between items-start gap-3 mb-6" aria-hidden="true">
          <div className="h-8 sm:h-9 bg-rule rounded-md w-52 animate-pulse" />
          <div className="h-9 bg-rule rounded-md w-24 animate-pulse" />
        </div>
        <div className="bg-card p-5 sm:p-6 rounded-lg border border-rule mb-4 animate-pulse" aria-hidden="true">
          <div className="h-3.5 bg-rule rounded w-20 mb-4" />
          <div className="space-y-3">
            <div className="h-3 bg-rule rounded w-64" />
            <div className="h-3 bg-rule rounded w-24" />
          </div>
        </div>
      </div>
    </main>
  );

  return (
    <main id="main-content" className="min-h-screen bg-page p-4 sm:p-8">
      <div className="max-w-4xl mx-auto">
        <div className="flex flex-wrap justify-between items-start gap-3 mb-6">
          <h1 className="text-2xl sm:text-3xl font-bold text-ink break-words min-w-0 flex-1">
            Welcome, {user.full_name}
          </h1>
          <button
            onClick={handleLogout}
            disabled={loggingOut}
            aria-busy={loggingOut}
            className="border border-wire text-dim px-4 py-2 rounded-md text-sm hover:border-dim hover:text-ink disabled:opacity-50 disabled:cursor-not-allowed transition-colors duration-150 shrink-0"
          >
            {loggingOut ? 'Signing out...' : 'Sign out'}
          </button>
        </div>

        <div className="bg-card p-5 sm:p-6 rounded-lg shadow-sm border border-rule mb-4">
          <h2 className="text-base font-semibold text-dim mb-3">Your profile</h2>
          <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-2 text-sm">
            <dt className="font-medium text-dim">Email</dt>
            <dd className="text-ink break-all">{user.email}</dd>
            <dt className="font-medium text-dim">Role</dt>
            <dd className="text-ink">{formatRole(user.role)}</dd>
          </dl>
        </div>

        <section aria-label="Portfolio dashboard" className="bg-card rounded-lg border border-rule overflow-hidden">
          <div className="px-6 py-10 sm:px-10 sm:py-12 flex flex-col items-center text-center">
            <div className="mb-5 text-notice-ink" aria-hidden="true">
              <svg width="40" height="34" viewBox="0 0 40 34" fill="none" xmlns="http://www.w3.org/2000/svg">
                <polyline
                  points="2,30 12,20 22,14 38,6"
                  stroke="currentColor"
                  strokeWidth="2.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
                <circle cx="12" cy="20" r="2.5" fill="currentColor" />
                <circle cx="22" cy="14" r="2.5" fill="currentColor" />
                <circle cx="38" cy="6" r="2.5" fill="currentColor" />
                <line x1="2" y1="33" x2="38" y2="33" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" opacity="0.35" />
              </svg>
            </div>

            <h2 className="text-lg font-semibold text-ink mb-2">
              Your portfolio dashboard is being set up
            </h2>

            <p className="text-sm text-dim mb-6" style={{ maxWidth: '44ch' }}>
              Once ready, you will see your holdings overview, performance metrics,
              asset allocation, and recent transaction history.
            </p>

            <div className="w-full max-w-xs border-t border-rule mb-5" />

            <p className="text-xs text-ghost">
              Configuration in progress. Contact your administrator if you have questions.
            </p>
          </div>
        </section>

        <p className="text-center text-xs text-ghost mt-8">
          IWS Finserv &copy; {new Date().getFullYear()} · Session expires after 30 minutes of inactivity
        </p>
      </div>
    </main>
  );
}
