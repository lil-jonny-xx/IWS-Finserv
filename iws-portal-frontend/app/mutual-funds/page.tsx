'use client';
import { useEffect, useState, useCallback, useRef } from 'react';
import { useRouter } from 'next/navigation';
import MFTable, { type MFHoldingRow, type MFTotals, type CombinedHolding } from './components/MFTable';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';
const TXN_PAGE_SIZE = 25;

interface User { role: string; full_name: string; entity_id?: number; }
interface Entity { id: number; name: string; }

interface Transaction {
  id: number;
  date: string;
  security_name: string;
  type: string;
  amount: number | null;
  units: number | null;
  nav: number | null;
  balance_units: number | null;
  folio_number: string;
  entity_name?: string;
}

interface TxnResponse {
  total: number;
  limit: number;
  offset: number;
  transactions: Transaction[];
}

interface HoldingsResponse {
  entity_id: number;
  entity_name: string;
  total_holdings: number;
  total_invested: number;
  holdings: MFHoldingRow[];
}

interface CombinedResponse {
  total_combined: number;
  total_invested: number;
  holdings: CombinedHolding[];
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

function fmtINR(n: number | null | undefined): string {
  if (n == null) return '—';
  const abs = Math.round(Math.abs(n));
  const str = abs.toString();
  const last3 = str.slice(-3);
  const rest = str.slice(0, -3);
  const grouped = rest.length > 0 ? rest.replace(/\B(?=(\d{2})+(?!\d))/g, ',') + ',' + last3 : last3;
  return (n < 0 ? '−₹' : '₹') + grouped;
}

function fmtDate(iso: string): string {
  return new Date(iso).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' });
}

const TXN_TYPE_COLOR: Record<string, string> = {
  PURCHASE: 'var(--gain)', PURCHASE_SIP: 'var(--gain)', SWITCH_IN: 'var(--gain)',
  REDEMPTION: 'var(--peril)', SWITCH_OUT: 'var(--peril)',
};

const TXN_TYPE_LABEL: Record<string, string> = {
  PURCHASE: 'Purchase', PURCHASE_SIP: 'SIP', SWITCH_IN: 'Switch In',
  SWITCH_OUT: 'Switch Out', REDEMPTION: 'Redemption',
  STAMP_DUTY_TAX: 'Stamp Duty', TDS_TAX: 'TDS Tax', STT_TAX: 'STT Tax',
};

function fmtTxnType(type: string): string {
  return TXN_TYPE_LABEL[type] ?? type;
}

export default function MutualFundsPage() {
  const router = useRouter();
  const [user, setUser]             = useState<User | null>(null);
  const [entities, setEntities]     = useState<Entity[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [data, setData]             = useState<HoldingsResponse | null>(null);
  const [loading, setLoading]       = useState(true);
  const [error, setError]           = useState<string | null>(null);
  const [retryCount, setRetryCount]     = useState(0);
  const [viewMode, setViewMode]         = useState<'normal' | 'combined'>('normal');
  const [combinedData, setCombinedData] = useState<CombinedResponse | null>(null);
  const [combinedLoading, setCombinedLoading] = useState(false);
  const [txnData, setTxnData]           = useState<TxnResponse | null>(null);
  const [txnPage, setTxnPage]       = useState(0);
  const [txnLoading, setTxnLoading] = useState(false);
  const [txnType, setTxnType]       = useState<string | null>(null);
  const didInitialLoad              = useRef(false);

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
    // Skeleton only on the first load; later fetches (entity switch / retry) update in
    // place without unmounting MFTable, so the user's sort/filters/search are preserved.
    if (!didInitialLoad.current) setLoading(true);
    setError(null);
    const qs = selectedId !== null ? `?entity_id=${selectedId}` : '';
    fetch(`${API_URL}/api/v1/holdings${qs}`, { credentials: 'include', signal: controller.signal })
      .then(r => { if (r.status === 401) { router.push('/'); return null; } if (!r.ok) throw new Error('Failed to load holdings.'); return r.json(); })
      .then((d: HoldingsResponse | null) => { if (d) setData(d); setLoading(false); didInitialLoad.current = true; })
      .catch(err => { if (err.name !== 'AbortError') { setError(err.message); setLoading(false); } });
    return () => controller.abort();
  }, [router, selectedId, retryCount]);

  useEffect(() => {
    const controller = new AbortController();
    setTxnLoading(true);
    const qs = new URLSearchParams({ limit: String(TXN_PAGE_SIZE), offset: String(txnPage * TXN_PAGE_SIZE) });
    if (selectedId !== null) qs.set('entity_id', String(selectedId));
    if (txnType) qs.set('txn_type', txnType);
    fetch(`${API_URL}/api/v1/transactions?${qs}`, { credentials: 'include', signal: controller.signal })
      .then(r => r.ok ? r.json() : null)
      .then((d: TxnResponse | null) => { if (d) setTxnData(d); setTxnLoading(false); })
      .catch(err => { if (err.name !== 'AbortError') setTxnLoading(false); });
    return () => controller.abort();
  }, [selectedId, txnPage, txnType]);

  useEffect(() => {
    if (viewMode !== 'combined') return;
    const controller = new AbortController();
    setCombinedLoading(true);
    fetch(`${API_URL}/api/v1/holdings/combined`, { credentials: 'include', signal: controller.signal })
      .then(r => { if (!r.ok) throw new Error('Failed to load combined holdings.'); return r.json(); })
      .then((d: CombinedResponse) => { setCombinedData(d); setCombinedLoading(false); })
      .catch(err => { if (err.name !== 'AbortError') setCombinedLoading(false); });
    return () => controller.abort();
  }, [viewMode]);

  function handleToggleCombined() {
    setViewMode(m => m === 'combined' ? 'normal' : 'combined');
  }

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
              { href: '/foreign-equity', label: 'Foreign Equity'        },
              { href: '/gold-silver', label: 'Gold/Silver' },
              { href: '/bank-accounts', label: 'Banks' },
              { href: '/pms', label: 'PMS' },
              { href: '/manual-data',  label: 'Manual Data'             },
              { href: '/reports',      label: 'Reports'                 },
              { href: '/benchmarks',   label: 'Benchmarks'              },
              { href: '/realised-gains', label: 'Realised Gains'        },
              { href: '/assistant',    label: 'Assistant'               },
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

        {/* Initial-load failure: no data to show, so surface the full error + retry. */}
        {error && !data && (
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

        {loading && !data && <Skeleton />}

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
            <MFTable
              holdings={data.holdings}
              totals={totals}
              showEntityCol={showEntityCol}
              viewMode={viewMode}
              onToggleCombined={handleToggleCombined}
              combinedHoldings={combinedData?.holdings}
              combinedTotals={combinedData ? { total_combined: combinedData.total_combined, total_invested: combinedData.total_invested } : undefined}
              filterResetKey={selectedId}
            />
          </div>
        )}

        {/* Transactions */}
        {txnData && (
          <div className="bg-card rounded-lg border border-rule overflow-hidden mt-6">
            <div className="px-5 sm:px-6 py-4 border-b border-rule">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h2 className="text-base font-semibold text-ink">Transactions</h2>
                  <p className="text-xs text-ghost mt-0.5">
                    {txnData.total === 0 ? 'No transactions' : `Showing ${txnData.offset + 1}–${Math.min(txnData.offset + txnData.transactions.length, txnData.total)} of ${txnData.total}`}
                  </p>
                </div>
                {txnData.total > TXN_PAGE_SIZE && (
                  <div className="flex items-center gap-2">
                    <button
                      disabled={txnPage === 0 || txnLoading}
                      onClick={() => setTxnPage(p => p - 1)}
                      className="px-3 py-1.5 text-xs border border-wire rounded text-dim hover:border-dim hover:text-ink disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                    >← Prev</button>
                    <span className="text-xs text-ghost tabular-nums">
                      {txnPage + 1} / {Math.ceil(txnData.total / TXN_PAGE_SIZE)}
                    </span>
                    <button
                      disabled={(txnPage + 1) * TXN_PAGE_SIZE >= txnData.total || txnLoading}
                      onClick={() => setTxnPage(p => p + 1)}
                      className="px-3 py-1.5 text-xs border border-wire rounded text-dim hover:border-dim hover:text-ink disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                    >Next →</button>
                  </div>
                )}
              </div>
              <div className="flex flex-wrap gap-1.5 mt-3">
                {([null, 'PURCHASE', 'PURCHASE_SIP', 'REDEMPTION', 'SWITCH_IN', 'SWITCH_OUT', 'STAMP_DUTY_TAX'] as (string | null)[]).map(t => {
                  const active = txnType === t;
                  return (
                    <button
                      key={t ?? 'all'}
                      onClick={() => { setTxnType(t); setTxnPage(0); }}
                      className={`px-2.5 py-1 rounded text-xs font-medium transition-colors ${
                        active
                          ? 'bg-prime text-prime-fg'
                          : 'bg-page border border-rule text-dim hover:border-dim hover:text-ink'
                      }`}
                    >
                      {t ? fmtTxnType(t) : 'All'}
                    </button>
                  );
                })}
              </div>
            </div>
            <div className="overflow-auto max-h-[60vh]">
              <table className="w-full text-xs" style={{ minWidth: '800px' }}>
                <thead>
                  <tr>
                    {['Date', 'Fund', ...(showEntityCol ? ['Entity'] : []), 'Folio', 'Type', 'Amount', 'Units', 'NAV', 'Balance'].map(h => (
                      <th key={h} className="px-3 py-2.5 text-left font-medium text-ghost bg-card border-b border-rule whitespace-nowrap sticky top-0 z-10 first:pl-5 last:pr-5">
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {txnLoading ? (
                    <tr><td colSpan={showEntityCol ? 9 : 8} className="px-5 py-6 text-center text-ghost">Loading…</td></tr>
                  ) : txnData.transactions.length === 0 ? (
                    <tr><td colSpan={showEntityCol ? 9 : 8} className="px-5 py-6 text-center text-ghost">No transactions found.</td></tr>
                  ) : txnData.transactions.map(t => {
                    const typeKey = t.type ?? '';
                    const color = TXN_TYPE_COLOR[typeKey] ?? 'var(--dim)';
                    return (
                      <tr key={t.id} className="border-t border-rule hover:bg-page transition-colors">
                        <td className="px-3 pl-5 py-3 whitespace-nowrap text-dim">{fmtDate(t.date)}</td>
                        <td className="px-3 py-3 text-ink max-w-[220px] truncate" title={t.security_name}>{t.security_name}</td>
                        {showEntityCol && <td className="px-3 py-3 text-dim whitespace-nowrap">{t.entity_name ?? '—'}</td>}
                        <td className="px-3 py-3 font-mono text-dim whitespace-nowrap">{t.folio_number}</td>
                        <td className="px-3 py-3 whitespace-nowrap font-medium" style={{ color }}>{t.type ? fmtTxnType(t.type) : '—'}</td>
                        <td className="px-3 py-3 text-right tabular-nums text-ink whitespace-nowrap">{fmtINR(t.amount)}</td>
                        <td className="px-3 py-3 text-right tabular-nums text-dim whitespace-nowrap">{t.units != null ? t.units.toFixed(3) : '—'}</td>
                        <td className="px-3 py-3 text-right tabular-nums text-dim whitespace-nowrap">{t.nav != null ? t.nav.toFixed(4) : '—'}</td>
                        <td className="px-3 pr-5 py-3 text-right tabular-nums text-ghost whitespace-nowrap">{t.balance_units != null ? t.balance_units.toFixed(3) : '—'}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}

        <p className="text-center text-xs text-ghost mt-8">
          IWS Finserv &copy; {new Date().getFullYear()} · NAV updated daily at 10:00 PM IST · Metrics at 10:15 PM IST
        </p>
      </div>
    </main>
  );
}
