'use client';
// Auto-logout after a period of inactivity. Mounted once in the root layout so it
// covers every authenticated page. Skips the login page.
//
// Approach: passive listeners just stamp a "last activity" time; a single interval
// checks elapsed idle time. This avoids thrashing a timer on every mousemove and —
// because it compares wall-clock time — correctly fires after the laptop sleeps/wakes
// (a background setTimeout would be throttled and unreliable there).
import { useEffect } from 'react';
import { usePathname, useRouter } from 'next/navigation';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';
const IDLE_MINUTES = Number(process.env.NEXT_PUBLIC_IDLE_MINUTES || 30);

const ACTIVITY_EVENTS = ['mousemove', 'mousedown', 'keydown', 'scroll', 'touchstart', 'click'];

export default function IdleTimeout() {
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    // No session to time out on the login page.
    if (pathname === '/') return;

    const idleMs = Math.max(1, IDLE_MINUTES) * 60_000;
    let last = Date.now();
    const bump = () => { last = Date.now(); };

    ACTIVITY_EVENTS.forEach(e => window.addEventListener(e, bump, { passive: true }));

    let loggingOut = false;
    const interval = setInterval(async () => {
      if (loggingOut || Date.now() - last < idleMs) return;
      loggingOut = true;
      clearInterval(interval);
      try {
        // Revoke the token server-side (Redis) and clear the cookie, then bounce to login.
        await fetch(`${API_URL}/api/v1/auth/logout`, { method: 'POST', credentials: 'include' });
      } catch {
        /* network hiccup — redirect anyway */
      }
      router.push('/');
    }, 15_000);

    return () => {
      clearInterval(interval);
      ACTIVITY_EVENTS.forEach(e => window.removeEventListener(e, bump));
    };
  }, [pathname, router]);

  return null;
}
