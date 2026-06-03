'use client';
import { useEffect, useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import MFTable, { type MFHoldingRow, type MFTotals } from './components/MFTable';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

interface User { role: string; full_name: string; entity_id?: number; }
interface Entity { id: number; name: string; }

interface HoldingsResponse {
  entity_id: number;
  entity_name: string;
  total_holdings: number;
  total_invested: number;
  holdings: MFHoldingRow[];
}


function EntitySwitcher({
  entities, selectedId, onSelect,
}: {
  entities: Entity[]; selectedId: number | null; onSelect: (id: number | null) => void;
}) {
  return (
    <div className="flex flex-wrap gap-1.5 mb-5" role="tablist" aria-label="Entity filter">
      {[{ id: null, name: 'All' }, ...entities.map(e => ({ id: e.id, name: e.name }))].map(tab => {
        const active = tab.id === selectedId;
        return (
          <button
            key={tab.id ?? 'all'}
            role="tab"
            aria-selected={active}
            onClick={() => onSelect(tab.id ?? null)}
            className={`px-3 py-1 rounded text-xs font-medium transition-colors ${
              active
                ? 'bg-prime text-prime-fg'
                : 'bg-card border border-rule text-dim hover:border-dim hover:text-ink'
            }`}
          >
            {tab.name}
          </button>
        );
      })}
    </div>
  );
}

function Skeleton() {
  return (
    <div className="bg-card rounded-lg border border-rule overflow-hidden" aria-hidden="true">
      <div className="px-5 sm:px-6 pt-5 pb-4 border-b border-rule">
        <div className="h-3.5 bg-rule rounded w-40 mb-4 animate-pulse" />
        <div className="flex flex-wrap gap-8">
          {[24, 20, 20, 20].map((_, i) => (
            <div key={i} className="space-y-1.5">
              <div className="h-2.5 bg-rule rounded w-20 animate-pulse" />
              <div className="h-4 bg-rule rounded w-24 animate-pulse" />
            </div>
          ))}
        </div>
      </div>
      {[...Array(8)].map((_, i) => (
        <div key={i} className="flex gap-4 px-5 sm:px-6 py-3.5 border-t border-rule">
          <div className="flex-1 space-y-1.5">
            <div className="h-3 bg-rule rounded max-w-xs animate-pulse" />
            <div className="h-2 bg-rule rounded w-16 animate-pulse" />
          </div>
          {[14, 18, 18, 16].map((w, j) => (
            <div key={j} className={`h-3 bg-rule rounded w-${w} animate-pulse self-center`} />
          ))}
        </div>
      ))}
    </div>
  );
}

export default function MutualFundsPage() {
  const router = useRouter();
  const [user, setUser]             = useState<User | null>(null);
  const [entities, setEntities]     = useState<Entity[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [data, setData]             = useState<HoldingsResponse | null>(null);
  const [loading, setLoading]       = useState(true);
  const [error, setError]           = useState<string | null>(null);
  const [retryCount, setRetryCount] = useState(0);

  useEffect(() => {
    fetch(`${API_URL}/api/v1/me`, { credentials: 'include' })
      .then(r => { if (r.status === 401) { router.push('/'); return null; } return r.json(); })
      .then((u: User | null) => {
        if (!u) return;
        setUser(u);
        if (u.role === 'admin') {
          fetch(`${API_URL}/api/v1/entities`, { credentials: 'include' })
            .then(r => r.ok ? r.json() : [])
            .then((ents: Entity[]) => setEntities(ents))
            .catch(() => {});
        }
      })
      .catch(() => router.push('/'));
  }, [router]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    const qs = selectedId !== null ? `?entity_id=${selectedId}` : '';
    fetch(`${API_URL}/api/v1/holdings${qs}`, { credentials: 'include', signal: controller.signal })
      .then(r => { if (r.status === 401) { router.push('/'); return null; } if (!r.ok) throw new Error('Failed to load holdings.'); return r.json(); })
      .then((d: HoldingsResponse | null) => { if (d) setData(d); setLoading(false); })
      .catch(err => { if (err.name !== 'AbortError') { setError(err.message); setLoading(false); } });
    return () => controller.abort();
  }, [router, selectedId, retryCount]);

  const isAdmin       = user?.role === 'admin';
  const showEntityCol = isAdmin && selectedId === null;
  const handleRetry   = useCallback(() => setRetryCount(c => c + 1), []);

  const totals: MFTotals = {
    total_holdings:     data?.total_holdings,
    total_invested:     data?.total_invested,
    total_current_value: data?.holdings.reduce((s, h) => s + (h.market_value_as_on ?? h.current_value ?? 0), 0),
  };

  return (
    <main id="main-content" className="min-h-screen bg-page p-4 sm:p-8">
      <div className="max-w-screen-2xl mx-auto">

        <div className="flex flex-wrap justify-between items-start gap-3 mb-6">
          <div>
            <h1 className="text-2xl sm:text-3xl font-bold text-ink">Mutual Fund Holdings</h1>
            <p className="text-sm text-ghost mt-0.5">All MF folios with weekly metrics</p>
          </div>
          <nav className="flex gap-1.5" aria-label="Sections">
            {[
              { href: '/dashboard',    label: 'Overview'                },
              { href: '/mutual-funds', label: 'Mutual Funds', active: true },
              { href: '/equity',       label: 'Equity'                  },
            ].map(({ href, label, active }) => (
              <a
                key={href}
                href={href}
                aria-current={active ? 'page' : undefined}
                className={`px-3 py-1.5 rounded text-xs font-medium transition-colors ${
                  active
                    ? 'bg-prime text-prime-fg'
                    : 'bg-card border border-rule text-dim hover:border-dim hover:text-ink'
                }`}
              >
                {label}
              </a>
            ))}
          </nav>
        </div>

        {isAdmin && entities.length > 0 && (
          <EntitySwitcher entities={entities} selectedId={selectedId} onSelect={setSelectedId} />
        )}

        {loading && <Skeleton />}

        {error && (
          <div role="alert" className="bg-card rounded-lg border border-rule px-5 py-5 flex items-start justify-between gap-4">
            <div>
              <p className="text-sm font-medium text-dim">Could not load holdings</p>
              <p className="text-xs text-ghost mt-1">{error}</p>
            </div>
            <button
              onClick={handleRetry}
              className="shrink-0 text-xs border border-wire text-dim px-3 py-1.5 rounded hover:border-dim hover:text-ink transition-colors"
            >
              Retry
            </button>
          </div>
        )}

        {!loading && !error && data && (
          <MFTable
            holdings={data.holdings}
            totals={totals}
            showEntityCol={showEntityCol}
          />
        )}

        <p className="text-center text-xs text-ghost mt-8">
          IWS Finserv &copy; {new Date().getFullYear()} · NAV updated daily at 10:00 PM IST · Metrics at 10:15 PM IST
        </p>
      </div>
    </main>
  );
}
