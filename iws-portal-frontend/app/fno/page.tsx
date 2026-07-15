'use client';
import { useEffect, useState, useCallback, useRef } from 'react';
import { useRouter } from 'next/navigation';
import NavTabs from '@/app/components/NavTabs';
import EntitySwitcher from '@/app/components/EntitySwitcher';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

interface User { role: string; full_name: string; entity_id?: number; }
interface Entity { id: number; name: string; }

interface FnoPosition {
  entity_id: number;
  entity_name: string;
  source: string;
  source_label: string;
  symbol: string;
  underlying: string | null;
  instrument: string | null;      // FUT | CE | PE
  expiry: string | null;
  strike: number | null;
  product: string | null;
  quantity: number;               // net; negative = short
  lot_size: number | null;
  avg_price: number | null;
  ltp: number | null;
  mtm_pnl: number | null;
  realized_pnl: number | null;
}

interface FnoAccount {
  entity_id: number;
  entity_name: string;
  source: string;
  source_label: string;
  margin_available: number | null;
  margin_used: number | null;
  ledger_balance: number | null;
  day_realized_pnl: number | null;
  total_mtm_pnl: number | null;
  as_of_date: string | null;
}

interface FnoResponse {
  entity_id: number;
  entity_name: string;
  positions: FnoPosition[];
  accounts: FnoAccount[];
  totals: {
    position_count: number;
    mtm_pnl: number;
    realized_pnl: number;
    margin_used: number;
    margin_available: number;
  };
  as_of_date: string | null;
  last_updated: string | null;
}

const inr = (v: number) => v.toLocaleString('en-IN', { maximumFractionDigits: 0 });
const signedINR = (v: number) => (v < 0 ? '−₹' : '₹') + inr(Math.abs(v));
const fmtINR = (v: number | null | undefined) => (v == null ? '—' : '₹' + inr(v));
const num = (v: number | null | undefined, dp = 2) =>
  v == null ? '—' : v.toLocaleString('en-IN', { maximumFractionDigits: dp });

function fmtDate(iso: string | null): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' });
}

function PnlCell({ value }: { value: number | null }) {
  if (value == null) return <span className="text-ghost">—</span>;
  return (
    <span className="font-medium tabular-nums" style={{ color: value >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
      {signedINR(value)}
    </span>
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
      {[...Array(5)].map((_, i) => (
        <div key={i} className="flex gap-4 px-5 sm:px-6 py-3.5 border-t border-rule">
          <div className="h-3 bg-rule rounded w-16 animate-pulse" />
          <div className="h-3 bg-rule rounded w-20 animate-pulse" />
          <div className="flex-1 h-3 bg-rule rounded animate-pulse" />
        </div>
      ))}
    </div>
  );
}

// Per-account margin / P&L strip (one card per entity+broker account).
function AccountCards({ accounts, showEntity }: { accounts: FnoAccount[]; showEntity: boolean }) {
  if (!accounts.length) return null;
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 mb-5">
      {accounts.map(a => (
        <div key={`${a.entity_id}-${a.source}`} className="bg-card rounded-lg border border-rule p-4">
          <div className="flex items-baseline justify-between gap-2 mb-3">
            <h3 className="text-sm font-semibold text-ink">{a.source_label}</h3>
            {showEntity && <span className="text-[11px] text-ghost">{a.entity_name}</span>}
          </div>
          <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
            <div>
              <dt className="text-ghost uppercase tracking-wide text-[10px]">Margin available</dt>
              <dd className="text-ink font-semibold tabular-nums mt-0.5">{fmtINR(a.margin_available)}</dd>
            </div>
            <div>
              <dt className="text-ghost uppercase tracking-wide text-[10px]">Margin used</dt>
              <dd className="text-ink font-semibold tabular-nums mt-0.5">{fmtINR(a.margin_used)}</dd>
            </div>
            <div>
              <dt className="text-ghost uppercase tracking-wide text-[10px]">MTM P&amp;L</dt>
              <dd className="mt-0.5"><PnlCell value={a.total_mtm_pnl} /></dd>
            </div>
            <div>
              <dt className="text-ghost uppercase tracking-wide text-[10px]">Day realised</dt>
              <dd className="mt-0.5"><PnlCell value={a.day_realized_pnl} /></dd>
            </div>
          </dl>
        </div>
      ))}
    </div>
  );
}

function PositionsTable({ positions, totals, showEntityCol, lastUpdated }: {
  positions: FnoPosition[];
  totals: FnoResponse['totals'];
  showEntityCol: boolean;
  lastUpdated: string | null;
}) {
  return (
    <div className="bg-card rounded-lg border border-rule overflow-hidden">
      <div className="px-5 sm:px-6 pt-5 pb-4 border-b border-rule flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-wrap gap-8">
          <div>
            <p className="text-[10px] uppercase tracking-wide text-ghost">Open positions</p>
            <p className="text-lg font-bold text-ink tabular-nums">{totals.position_count}</p>
          </div>
          <div>
            <p className="text-[10px] uppercase tracking-wide text-ghost">MTM P&amp;L</p>
            <p className="text-lg font-bold tabular-nums"
               style={{ color: totals.mtm_pnl >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
              {signedINR(totals.mtm_pnl)}
            </p>
          </div>
          <div>
            <p className="text-[10px] uppercase tracking-wide text-ghost">Margin used</p>
            <p className="text-lg font-bold text-ink tabular-nums">{fmtINR(totals.margin_used)}</p>
          </div>
        </div>
        {lastUpdated && (
          <p className="text-[11px] text-ghost">Updated {new Date(lastUpdated).toLocaleString('en-IN', {
            day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
          })}</p>
        )}
      </div>
      {positions.length === 0 ? (
        <div className="px-6 py-10 text-center">
          <p className="text-sm font-medium text-dim">No open FnO positions</p>
          <p className="text-xs text-ghost mt-1">
            Positions appear here after the Share India / Orbis portal sync runs.
          </p>
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="bg-page text-dim">
                {showEntityCol && <th className="px-3 py-2 text-left font-semibold">Entity</th>}
                <th className="px-3 py-2 text-left font-semibold">Broker</th>
                <th className="px-3 py-2 text-left font-semibold">Contract</th>
                <th className="px-3 py-2 text-left font-semibold">Type</th>
                <th className="px-3 py-2 text-left font-semibold">Expiry</th>
                <th className="px-3 py-2 text-right font-semibold">Strike</th>
                <th className="px-3 py-2 text-right font-semibold">Net Qty</th>
                <th className="px-3 py-2 text-right font-semibold">Avg</th>
                <th className="px-3 py-2 text-right font-semibold">LTP</th>
                <th className="px-3 py-2 text-right font-semibold">MTM P&amp;L</th>
                <th className="px-3 py-2 text-right font-semibold">Realised</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p, i) => {
                const short = p.quantity < 0;
                return (
                  <tr key={i} className="border-t border-rule">
                    {showEntityCol && <td className="px-3 py-2 text-dim">{p.entity_name}</td>}
                    <td className="px-3 py-2 text-ghost">{p.source_label}</td>
                    <td className="px-3 py-2 text-ink font-medium">
                      {p.symbol}
                      {p.product && <span className="ml-1.5 text-[10px] text-ghost">{p.product}</span>}
                    </td>
                    <td className="px-3 py-2">
                      {p.instrument ? (
                        <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold border border-rule text-dim">
                          {p.instrument}
                        </span>
                      ) : <span className="text-ghost">—</span>}
                    </td>
                    <td className="px-3 py-2 text-dim">{fmtDate(p.expiry)}</td>
                    <td className="px-3 py-2 text-right text-dim tabular-nums">{num(p.strike)}</td>
                    <td className="px-3 py-2 text-right tabular-nums font-medium"
                        style={{ color: short ? 'var(--peril)' : 'var(--ink)' }}>
                      {num(p.quantity, 0)}
                    </td>
                    <td className="px-3 py-2 text-right text-dim tabular-nums">{num(p.avg_price)}</td>
                    <td className="px-3 py-2 text-right text-dim tabular-nums">{num(p.ltp)}</td>
                    <td className="px-3 py-2 text-right"><PnlCell value={p.mtm_pnl} /></td>
                    <td className="px-3 py-2 text-right"><PnlCell value={p.realized_pnl} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default function FnoPage() {
  const router = useRouter();
  const [user, setUser]             = useState<User | null>(null);
  const [entities, setEntities]     = useState<Entity[]>([]);
  const [selectedIds, setSelectedIds] = useState<number[]>([]);   // empty = All; >1 = subset
  const selKey = selectedIds.join(',');
  const toggleEntity = useCallback((id: number | null) => {
    if (id === null) { setSelectedIds([]); return; }
    setSelectedIds(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);
  }, []);
  const [data, setData]             = useState<FnoResponse | null>(null);
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
        fetch(`${API_URL}/api/v1/entities`, { credentials: 'include' })
          .then(r => r.ok ? r.json() : [])
          .then((ents: Entity[]) => setEntities(ents))
          .catch(() => {});
      })
      .catch(() => router.push('/'));
  }, [router]);

  useEffect(() => {
    const controller = new AbortController();
    if (!didInitialLoad.current) setLoading(true);
    setError(null);
    const qs = selectedIds.length ? '?' + selectedIds.map(id => `entity_id=${id}`).join('&') : '';
    fetch(`${API_URL}/api/v1/fno/positions${qs}`, { credentials: 'include', signal: controller.signal })
      .then(r => { if (r.status === 401) { router.push('/'); return null; } if (!r.ok) throw new Error('Failed to load FnO positions.'); return r.json(); })
      .then((d: FnoResponse | null) => {
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

  return (
    <main id="main-content" className="min-h-screen bg-page p-4 sm:p-8">
      <div className="max-w-screen-2xl mx-auto">

        <div className="flex flex-wrap justify-between items-start gap-3 mb-6">
          <div>
            <h1 className="text-2xl sm:text-3xl font-bold text-ink">FnO</h1>
            <div className="flex items-center gap-2 mt-0.5">
              <span className="text-sm text-ghost">Futures &amp; options positions (Share India, Orbis)</span>
              {data?.as_of_date && (
                <span className="text-xs text-ghost">· as of {fmtDate(data.as_of_date)}</span>
              )}
            </div>
          </div>
          <NavTabs active="/fno" role={user?.role} />
        </div>

        {isAdmin && entities.length > 0 && (
          <EntitySwitcher section="/fno" entities={entities} selectedIds={selectedIds} onToggle={toggleEntity} />
        )}

        {loading && !data && <Skeleton />}

        {error && !data && (
          <div role="alert" className="bg-card rounded-lg border border-rule px-5 py-5 flex items-start justify-between gap-4">
            <div>
              <p className="text-sm font-medium text-dim">Could not load FnO positions</p>
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
            <AccountCards accounts={data.accounts} showEntity={showEntityCol} />
            <PositionsTable
              positions={data.positions}
              totals={data.totals}
              showEntityCol={showEntityCol}
              lastUpdated={data.last_updated}
            />
          </div>
        )}

        <p className="text-center text-xs text-ghost mt-8">
          IWS Finserv &copy; {new Date().getFullYear()} · FnO positions update on each portal sync
        </p>
      </div>
    </main>
  );
}
