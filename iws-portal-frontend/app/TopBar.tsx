'use client';
// The one global bar: markets ticker on the left, session controls on the right.
// Mounted once in the root layout so Sign out is reachable from every page —
// it used to exist only in the dashboard's own header, which meant you had to
// navigate back to Overview to sign out.
//
// Deliberately does NOT depend on the benchmark feed: BenchmarkTicker renders
// nothing when it has no data, and the sign-out has to survive that.
//
// Auth state is inferred from the route, the same way IdleTimeout does it — the
// login page is the only unauthenticated route, and every other route already
// bounces to '/' on a 401.
import { useCallback, useState } from 'react';
import { usePathname, useRouter } from 'next/navigation';
import BenchmarkTicker from './BenchmarkTicker';
import { useMe } from '@/app/lib/useMe';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

const HIDDEN_ROUTES = new Set(['/', '/forgot-password']);

export default function TopBar() {
  const pathname = usePathname();
  const router = useRouter();
  const [loggingOut, setLoggingOut] = useState(false);

  const hidden = HIDDEN_ROUTES.has(pathname);
  // Shared with GlobalNav so the two bars don't both fetch the session.
  const user = useMe(!hidden);

  const handleLogout = useCallback(async () => {
    setLoggingOut(true);
    try {
      await fetch(`${API_URL}/api/v1/auth/logout`, { method: 'POST', credentials: 'include' });
    } catch {
      /* network hiccup — bounce to login anyway */
    }
    router.push('/');
  }, [router]);

  if (hidden) return null;

  return (
    <div
      style={{
        // Stickiness is owned by StickyChrome, which sticks this bar and the nav
        // together — see the note there on why they can't stick independently.
        background: 'var(--ink)', color: 'var(--card)',
        borderBottom: '1px solid var(--rule)',
      }}
      className="w-full flex items-center"
    >
      <div className="min-w-0 flex-1">
        <BenchmarkTicker />
      </div>
      <div className="flex items-center gap-3 px-4 py-1.5 text-xs shrink-0">
        {user?.full_name || user?.email ? (
          <span className="hidden sm:block" style={{ opacity: 0.6 }}>
            {user.full_name || user.email}
          </span>
        ) : null}
        <button
          onClick={handleLogout}
          disabled={loggingOut}
          className="font-medium transition-opacity disabled:opacity-50 hover:opacity-100"
          style={{ opacity: 0.75 }}
        >
          {loggingOut ? 'Signing out…' : 'Sign out'}
        </button>
      </div>
    </div>
  );
}
