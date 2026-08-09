'use client';
import { useEffect, useState, useCallback, useRef } from 'react';
import { useRouter } from 'next/navigation';
import { Glass } from '@/app/components/PrivacyGlass';
import EntitySwitcher from '@/app/components/EntitySwitcher';
import CommodityTable, { type CommodityHoldingRow, type CommodityTotals } from './components/CommodityTable';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

interface User { role: string; full_name: string; entity_id?: number; }
interface Entity { id: number; name: string; }

interface GoldSilverResponse {
  entity_id: number;
  entity_name: string;
  total_holdings: number;
  holdings: CommodityHoldingRow[];
  // The API also splits the same rows into metals / commodities; the page keeps
  // `holdings` as the single source for the table (asset class is a filter there)
  // and uses the pre-summed totals only for the headline cards.
  metals: CommodityHoldingRow[];
  commodities: CommodityHoldingRow[];
  metals_total: number;
  commodities_total: number;
  totals: CommodityTotals;
  fx_rates: Record<string, number>;
}

function fmtINR(n: number | null | undefined): string {
  if (n == null) return '—';
  return '₹' + Math.round(n).toLocaleString('en-IN');
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

export default function CommoditiesPage() {
  const router = useRouter();
  const [user, setUser]               = useState<User | null>(null);
  const [entities, setEntities]       = useState<Entity[]>([]);
  // Multi-entity selection; empty = All. The backend scopes with = ANY(), so the
  // returned totals already reflect the chosen subset.
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const selKey = selectedIds.join(',');
  const toggleEntity = useCallback((id: number | null) => {
    if (id === null) { setSelectedIds([]); return; }        // "All" clears
    setSelectedIds(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);
  }, []);
  const [data, setData]               = useState<GoldSilverResponse | null>(null);
  const [loading, setLoading]         = useState(true);
  const [error, setError]             = useState<string | null>(null);
  const [retryCount, setRetryCount]   = useState(0);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [liveActive, setLiveActive]   = useState(false);
  const intervalRef                   = useRef<ReturnType<typeof setInterval> | null>(null);
  const didInitialLoad                = useRef(false);

  useEffect(() => {
    fetch(`${API_URL}/api/v1/me`, { credentials: 'include' })
      .then(r => { if (r.status === 401) { router.push('/'); return null; } return r.json(); })
      .then((u: User | null) => {
        if (!u) return;
        setUser(u);
        fetch(`${API_URL}/api/v1/entities`, { credentials: 'include' })
          .then(r => r.ok ? r.json() : [])
          .then((ents: Entity[]) => setEntities(ents))
          .catch(() => {});
      })
      .catch(() => router.push('/'));
  }, [router]);

  useEffect(() => {
    const controller = new AbortController();
    // Show the skeleton only on the very first load. Background refreshes update
    // values in place without unmounting the table, so the user's sort / filters /
    // search survive a refresh.
    if (!didInitialLoad.current) setLoading(true);
    setError(null);
    const qs = selectedIds.length ? '?' + selectedIds.map(id => `entity_id=${id}`).join('&') : '';
    fetch(`${API_URL}/api/v1/gold-silver/holdings${qs}`, { credentials: 'include', signal: controller.signal })
      .then(r => { if (r.status === 401) { router.push('/'); return null; } if (!r.ok) throw new Error('Failed to load commodity holdings.'); return r.json(); })
      .then((d: GoldSilverResponse | null) => {
        if (d) { setData(d); setLastUpdated(new Date()); }
        setLoading(false);
        didInitialLoad.current = true;
      })
      .catch(err => { if (err.name !== 'AbortError') { setError(err.message); setLoading(false); } });
    return () => controller.abort();
  }, [router, selKey, retryCount]);   // eslint-disable-line react-hooks/exhaustive-deps

  // Auto-refresh every 60 s during market hours (09:15–15:30 IST Mon–Fri) — gold and
  // silver ETFs are exchange-traded, so they move with the rest of the equity book.
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
  }, [selKey]);

  const isAdmin       = !!user;  // members have admin-level view access
  const showEntityCol = isAdmin && selectedIds.length !== 1;
  const handleRetry   = useCallback(() => setRetryCount(c => c + 1), []);

  const grandTotal = data ? (data.metals_total + data.commodities_total) : 0;

  return (
    <main id="main-content" className="min-h-screen bg-page py-4 sm:py-8">
      <div className="shell">

        <div className="flex flex-wrap justify-between items-start gap-3 mb-6">
          <div>
            <h1 className="text-2xl sm:text-3xl font-bold text-ink">Commodities</h1>
            <div className="flex items-center gap-2 mt-0.5">
              {liveActive ? (
                <span className="flex items-center gap-1.5 text-xs text-gain font-medium">
                  <span className="w-1.5 h-1.5 rounded-full bg-gain animate-pulse inline-block" />
                  Live · updates every minute
                </span>
              ) : (
                <span className="text-sm text-ghost">
                  Precious metals (gold &amp; silver ETFs, sovereign gold bonds) and commodities
                </span>
              )}
              {lastUpdated && (
                <span className="text-xs text-ghost">
                  · updated {lastUpdated.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
                </span>
              )}
            </div>
          </div>
        </div>

        {isAdmin && entities.length > 0 && (
          <EntitySwitcher section="/gold-silver" entities={entities} selectedIds={selectedIds} onToggle={toggleEntity} />
        )}

        {loading && !data && <Skeleton />}

        {/* Initial-load failure: no data to show, so surface the full error + retry. */}
        {error && !data && (
          <div role="alert" className="bg-card rounded-lg border border-rule px-5 py-5 flex items-start justify-between gap-4">
            <div>
              <p className="text-sm font-medium text-dim">Could not load commodity holdings</p>
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

            {/* Headline split — the metals-vs-commodities view the page used to render
                as two separate tables. It's a filter on the table below now, so the
                split lives here as a summary instead. */}
            <Glass className="mb-6">
              <div className="bg-card rounded-lg border border-rule px-5 sm:px-6 py-4 flex flex-wrap gap-8 items-end">
                <div>
                  <p className="text-[11px] uppercase tracking-wide text-ghost">Total Gold / Silver &amp; Commodities</p>
                  <p className="text-2xl font-bold text-ink tabular-nums">{fmtINR(grandTotal)}</p>
                </div>
                <div>
                  <p className="text-[11px] uppercase tracking-wide text-ghost">Precious Metals</p>
                  <p className="text-base font-semibold text-ink tabular-nums">{fmtINR(data.metals_total)}</p>
                </div>
                <div>
                  <p className="text-[11px] uppercase tracking-wide text-ghost">Commodities</p>
                  <p className="text-base font-semibold text-ink tabular-nums">{fmtINR(data.commodities_total)}</p>
                </div>
              </div>
            </Glass>

            <CommodityTable
              holdings={data.holdings}
              totals={data.totals ?? {}}
              showEntityCol={showEntityCol}
            />
          </div>
        )}

        <p className="text-center text-xs text-ghost mt-8">
          Rajani MIS &copy; {new Date().getFullYear()} · Live prices during market hours (09:15–15:30 IST)
        </p>
      </div>
    </main>
  );
}
