'use client';
import { useEffect, useState } from 'react';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

// /api/v1/nav-coverage — which entities have data in each nav section, plus
// every Manual Data category with entries. One fetch per page load (deduped),
// session-cached so tabs and entity pills settle instantly on later pages.
export interface NavCoverage {
  sections: Record<string, number[]>;    // href → entity ids with data
  categories: Record<string, number[]>;  // manual category → entity ids
}

const CACHE_KEY = 'iws-nav-coverage';
let inflight: Promise<NavCoverage | null> | null = null;

export function cachedNavCoverage(): NavCoverage | null {
  try {
    const raw = sessionStorage.getItem(CACHE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
}

export function fetchNavCoverage(): Promise<NavCoverage | null> {
  inflight ??= fetch(`${API_URL}/api/v1/nav-coverage`, { credentials: 'include' })
    .then(r => (r.ok ? (r.json() as Promise<NavCoverage>) : null))
    .then(d => {
      if (d) { try { sessionStorage.setItem(CACHE_KEY, JSON.stringify(d)); } catch { /* quota */ } }
      return d;
    })
    .catch(() => null);
  return inflight;
}

export function useNavCoverage(): NavCoverage | null {
  const [cov, setCov] = useState<NavCoverage | null>(null);
  useEffect(() => {
    const cached = cachedNavCoverage();
    if (cached) setCov(cached);
    let on = true;
    fetchNavCoverage().then(d => { if (on && d) setCov(d); });
    return () => { on = false; };
  }, []);
  return cov;
}

// Entity pills for one section: drop entities with no data there. While
// coverage is still unknown (ids undefined), show everything rather than
// flash an empty row.
export function entitiesWithData<T extends { id: number }>(
  entities: T[], ids: number[] | undefined,
): T[] {
  if (!ids) return entities;
  const s = new Set(ids);
  return entities.filter(e => s.has(e.id));
}
