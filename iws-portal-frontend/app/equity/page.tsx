'use client';
import { useEffect, useState, useCallback, useRef } from 'react';
import { useRouter } from 'next/navigation';
import EquityTable, { type EquityHoldingRow, type EquityTotals } from './components/EquityTable';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

interface User { role: string; full_name: string; entity_id?: number; }
interface Entity { id: number; name: string; }

interface EquityResponse {
  entity_id: number;
  entity_name: string;
  total_holdings: number;
  holdings: EquityHoldingRow[];
  totals: EquityTotals;
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
        <div className="h-3.5 bg-rule rounded w-32 mb-4 animate-pulse" />
        <div className="flex flex-wrap gap-8">
          {[24, 20, 20].map((w, i) => (
            <div key={i} className="space-y-1.5">
              <div className={`h-2.5 bg-rule rounded w-${w} animate-pulse`} />
              <div className="h-4 bg-rule rounded w-24 animate-pulse" />
            </div>
          ))}
        </div>
      </div>
      {[...Array(6)].map((_, i) => (
        <div key={i} className="flex gap-4 px-5 sm:px-6 py-3.5 border-t border-rule">
          <div className="h-3 bg-rule rounded w-16 animate-pulse" />
          <div className="h-3 bg-rule rounded w-20 animate-pulse" />
          <div className="flex-1 h-3 bg-rule rounded animate-pulse" />
        </div>
      ))}
    </div>
  );
}

export default function EquityPage() {
  const router = useRouter();
  const [user, setUser]                   = useState<User | null>(null);
  const [entities, setEntities]           = useState<Entity[]>([]);
  const [selectedId, setSelectedId]       = useState<number | null>(null);
  const [data, setData]                   = useState<EquityResponse | null>(null);
  const [loading, setLoading]             = useState(true);
  const [error, setError]                 = useState<string | null>(null);
  const [retryCount, setRetryCount]       = useState(0);
  const [lastUpdated, setLastUpdated]     = useState<Date | null>(null);
  const [liveActive, setLiveActive]       = useState(false);
  const intervalRef                       = useRef<ReturnType<typeof setInterval> | null>(null);
  const didInitialLoad                    = useRef(false);

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
    // Show the skeleton only on the very first load. Background refreshes (auto-refresh
    // every minute) update values in place without unmounting the table, so the user's
    // sort / filters / search are preserved.
    if (!didInitialLoad.current) setLoading(true);
    setError(null);
    const qs = selectedId !== null ? `?entity_id=${selectedId}` : '';
    fetch(`${API_URL}/api/v1/equity/holdings${qs}`, { credentials: 'include', signal: controller.signal })
      .then(r => { if (r.status === 401) { router.push('/'); return null; } if (!r.ok) throw new Error('Failed to load equity holdings.'); return r.json(); })
      .then((d: EquityResponse | null) => {
        if (d) { setData(d); setLastUpdated(new Date()); }
        setLoading(false);
        didInitialLoad.current = true;
      })
      .catch(err => { if (err.name !== 'AbortError') { setError(err.message); setLoading(false); } });
    return () => controller.abort();
  }, [router, selectedId, retryCount]);

  // Auto-refresh every 60 s during market hours (09:15–15:30 IST Mon–Fri)
  useEffect(() => {
    function isMarketOpen() {
      const now = new Date();
      const ist = new Date(now.toLocaleString('en-US', { timeZone: 'Asia/Kolkata' }));
      const day = ist.getDay();
      if (day === 0 || day === 6) return false;
      const mins = ist.getHours() * 60 + ist.getMinutes();
      return mins >= 9 * 60 + 15 && mins < 15 * 60 + 30;
    }

    if (intervalRef.current) clearInterval(intervalRef.current);
    const active = isMarketOpen();
    setLiveActive(active);
    if (!active) return;

    intervalRef.current = setInterval(() => {
      if (!isMarketOpen()) { clearInterval(intervalRef.current!); setLiveActive(false); return; }
      setRetryCount(c => c + 1);
    }, 60_000);

    return () => { if (intervalRef.current) clearInterval(intervalRef.current); };
  }, [selectedId]);

  const isAdmin       = user?.role === 'admin';
  const showEntityCol = isAdmin && selectedId === null;
  const handleRetry   = useCallback(() => setRetryCount(c => c + 1), []);

  return (
    <main id="main-content" className="min-h-screen bg-page p-4 sm:p-8">
      <div className="max-w-screen-2xl mx-auto">

        <div className="flex flex-wrap justify-between items-start gap-3 mb-6">
          <div>
            <h1 className="text-2xl sm:text-3xl font-bold text-ink">Equity Portfolio</h1>
            <div className="flex items-center gap-2 mt-0.5">
              {liveActive ? (
                <span className="flex items-center gap-1.5 text-xs text-gain font-medium">
                  <span className="w-1.5 h-1.5 rounded-full bg-gain animate-pulse inline-block" />
                  Live · updates every minute
                </span>
              ) : (
                <span className="text-sm text-ghost">Direct stock holdings across all brokers</span>
              )}
              {lastUpdated && (
                <span className="text-xs text-ghost">
                  · updated {lastUpdated.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
                </span>
              )}
            </div>
          </div>
          <nav className="flex gap-1.5" aria-label="Sections">
            {[
              { href: '/dashboard',    label: 'Overview'     },
              { href: '/mutual-funds', label: 'Mutual Funds' },
              { href: '/equity',       label: 'Equity',       active: true },
              { href: '/pms', label: 'PMS' },
              { href: '/manual-data',  label: 'Manual Data'  },
              { href: '/reports',      label: 'Reports'       },
              { href: '/benchmarks',   label: 'Benchmarks'    },
              { href: '/realised-gains', label: 'Realised Gains' },
              { href: '/assistant',    label: 'Assistant'     },
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

        {loading && !data && <Skeleton />}

        {/* Initial-load failure: no data to show, so surface the full error + retry. */}
        {error && !data && (
          <div role="alert" className="bg-card rounded-lg border border-rule px-5 py-5 flex items-start justify-between gap-4">
            <div>
              <p className="text-sm font-medium text-dim">Could not load equity holdings</p>
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

        {data && (
          <div className="fade-in">
            {/* Background-refresh failure: keep the last-loaded table, show a quiet notice. */}
            {error && (
              <div role="status" className="mb-3 flex items-center justify-between gap-3 bg-card rounded-lg border border-rule px-4 py-2">
                <p className="text-xs text-ghost">Couldn’t refresh — showing last loaded values.</p>
                <button
                  onClick={handleRetry}
                  className="shrink-0 text-xs border border-wire text-dim px-2.5 py-1 rounded hover:border-dim hover:text-ink transition-colors"
                >
                  Retry
                </button>
              </div>
            )}
            <EquityTable
              holdings={data.holdings}
              totals={data.totals}
              showEntityCol={showEntityCol}
            />
          </div>
        )}

        <p className="text-center text-xs text-ghost mt-8">
          IWS Finserv &copy; {new Date().getFullYear()} · Live prices during market hours (09:15–15:30 IST)
        </p>
      </div>
    </main>
  );
}
