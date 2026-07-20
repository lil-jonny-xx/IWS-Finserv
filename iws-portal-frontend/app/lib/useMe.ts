'use client';
// Shared /api/v1/me fetch for the always-mounted chrome (TopBar, GlobalNav).
//
// Both need the session — TopBar for the name and Sign out, GlobalNav for the
// role that decides which tabs show — and mounting them together would otherwise
// fire the same request twice on every page load. The in-flight promise is cached
// at module scope so the second caller joins the first.
//
// Navigation between sections is a full document load (NavTabs renders plain <a>,
// not next/link), so the cache is naturally per-page-view — there is no staleness
// to invalidate.
import { useEffect, useState } from 'react';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

export interface Me {
  role?: string;
  full_name?: string;
  email?: string;
  entity_id?: number;
}

let inflight: Promise<Me | null> | null = null;

function fetchMe(): Promise<Me | null> {
  if (!inflight) {
    inflight = fetch(`${API_URL}/api/v1/me`, { credentials: 'include' })
      .then(r => (r.ok ? r.json() : null))
      .catch(() => null);   // offline — the chrome still has to render
  }
  return inflight;
}

/** null while loading or unauthenticated; `enabled: false` skips the call entirely. */
export function useMe(enabled = true): Me | null {
  const [me, setMe] = useState<Me | null>(null);

  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    fetchMe().then(d => { if (alive) setMe(d); });
    return () => { alive = false; };
  }, [enabled]);

  // Masked rather than cleared on disable — clearing would mean a setState in the
  // effect body, which cascades a render for no gain.
  return enabled ? me : null;
}
