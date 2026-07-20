'use client';
import { useEffect, useState, useCallback, useRef } from 'react';
import { useRouter } from 'next/navigation';
import EntitySwitcher from '@/app/components/EntitySwitcher';
import EquityTable, { type EquityHoldingRow, type EquityTotals } from '@/app/equity/components/EquityTable';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

interface User { role: string; full_name: string; entity_id?: number; }
interface Entity { id: number; name: string; }

interface HoldingRow {
  entity_id: number;
  entity_name: string | null;
  broker: string;
  symbol: string;
  sector: string | null;
  asset_class: string;
  quantity: number | null;
  cost: number | null;
  current_market_value: number | null;
  current_market_value_native: number | null;
  currency: string;
  pnl_inception: number | null;
  returns_inception_pct: number | null;
}

interface GoldSilverResponse {
  entity_id: number;
  entity_name: string;
  total_holdings: number;
  holdings: HoldingRow[];
  metals: HoldingRow[];
  commodities: HoldingRow[];
  metals_total: number;
  commodities_total: number;
  metals_totals: EquityTotals;
  commodities_totals: EquityTotals;
  fx_rates: Record<string, number>;
}

function fmtINR(n: number | null | undefined): string {
  if (n == null) return '—';
  return '₹' + Math.round(n).toLocaleString('en-IN');
}

export default function GoldSilverPage() {
  const router = useRouter();
  const [user, setUser]             = useState<User | null>(null);
  const [entities, setEntities]     = useState<Entity[]>([]);
  const [selectedIds, setSelectedIds] = useState<number[]>([]);   // empty = All; >1 = subset
  const selKey = selectedIds.join(',');
  const toggleEntity = useCallback((id: number | null) => {
    if (id === null) { setSelectedIds([]); return; }
    setSelectedIds(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);
  }, []);
  const [data, setData]             = useState<GoldSilverResponse | null>(null);
  const [loading, setLoading]       = useState(true);
  const [error, setError]           = useState<string | null>(null);
  const [retryCount, setRetryCount] = useState(0);
  const didInitialLoad              = useRef(false);

  useEffect(() => {
    fetch(`${API_URL}/api/v1/me`, { credentials: 'include' })
      .then(r => { if (r.status === 401) { router.push('/'); return null; } return r.json(); })
      .then((u: User | null) => {
        if (!u) return;
        setUser(u);
        if (u) {
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
    if (!didInitialLoad.current) setLoading(true);
    setError(null);
    const qs = selectedIds.length ? '?' + selectedIds.map(id => `entity_id=${id}`).join('&') : '';
    fetch(`${API_URL}/api/v1/gold-silver/holdings${qs}`, { credentials: 'include', signal: controller.signal })
      .then(r => { if (r.status === 401) { router.push('/'); return null; } if (!r.ok) throw new Error('Failed to load gold/silver holdings.'); return r.json(); })
      .then((d: GoldSilverResponse | null) => {
        if (d) setData(d);
        setLoading(false);
        didInitialLoad.current = true;
      })
      .catch(err => { if (err.name !== 'AbortError') { setError(err.message); setLoading(false); } });
    return () => controller.abort();
  }, [router, selKey, retryCount]);   // eslint-disable-line react-hooks/exhaustive-deps

  const isAdmin       = !!user;  // members have admin-level view access (only Manual Data + user mgmt are admin-only)
  const showEntityCol = isAdmin && selectedIds.length !== 1;
  const handleRetry   = useCallback(() => setRetryCount(c => c + 1), []);

  const grandTotal = data ? (data.metals_total + data.commodities_total) : 0;

  return (
    <main id="main-content" className="min-h-screen bg-page p-4 sm:p-8">
      <div className="max-w-screen-2xl mx-auto">

        <div className="flex flex-wrap justify-between items-start gap-3 mb-6">
          <div>
            <h1 className="text-2xl sm:text-3xl font-bold text-ink">Commodities</h1>
            <span className="text-sm text-ghost">Precious metals (gold &amp; silver ETFs, sovereign gold bonds) and commodities</span>
          </div>
        </div>

        {isAdmin && entities.length > 0 && (
          <EntitySwitcher section="/gold-silver" entities={entities} selectedIds={selectedIds} onToggle={toggleEntity} />
        )}

        {loading && !data && (
          <div className="bg-card rounded-lg border border-rule px-5 py-16 text-center text-sm text-ghost">Loading…</div>
        )}

        {error && !data && (
          <div role="alert" className="bg-card rounded-lg border border-rule px-5 py-5 flex items-start justify-between gap-4">
            <div>
              <p className="text-sm font-medium text-dim">Could not load gold/silver holdings</p>
              <p className="text-xs text-ghost mt-1">{error}</p>
            </div>
            <button onClick={handleRetry} className="shrink-0 text-xs border border-wire text-dim px-3 py-1.5 rounded hover:border-dim hover:text-ink transition-colors">Retry</button>
          </div>
        )}

        {data && (
          <div className="fade-in">
            {/* Combined total */}
            <div className="bg-card rounded-lg border border-rule px-5 sm:px-6 py-4 mb-6 flex flex-wrap gap-8 items-end">
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

            {data.total_holdings === 0 && (
              <div className="bg-card rounded-lg border border-rule px-5 py-16 text-center text-sm text-ghost">
                No gold, silver or commodity holdings yet. New broker buys appear here automatically after the next sync.
              </div>
            )}

            {/* Same table component the Equity tab uses, so every metric computed
                for equity (YTD, CAGR, XIRR, FY growth, weekly change, exposure)
                appears here too, with its sorting, filtering and totals footer. */}
            {data.metals.length > 0 && (
              <section className="mb-6">
                <h2 className="text-base font-semibold text-ink mb-1">Precious Metals</h2>
                <p className="text-xs text-ghost mb-2">Gold &amp; silver ETFs and sovereign gold bonds</p>
                <EquityTable
                  holdings={data.metals as unknown as EquityHoldingRow[]}
                  totals={data.metals_totals}
                  showEntityCol={showEntityCol}
                />
              </section>
            )}
            {data.commodities.length > 0 && (
              <section>
                <h2 className="text-base font-semibold text-ink mb-1">Commodities</h2>
                <p className="text-xs text-ghost mb-2">Uranium and other commodity instruments (incl. international)</p>
                <EquityTable
                  holdings={data.commodities as unknown as EquityHoldingRow[]}
                  totals={data.commodities_totals}
                  showEntityCol={showEntityCol}
                />
              </section>
            )}
          </div>
        )}

        <p className="text-center text-xs text-ghost mt-8">
          Rajani MIS &copy; {new Date().getFullYear()} · Updates on each broker sync
        </p>
      </div>
    </main>
  );
}
