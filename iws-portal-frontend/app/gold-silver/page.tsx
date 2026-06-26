'use client';
import { useEffect, useState, useCallback, useRef } from 'react';
import { useRouter } from 'next/navigation';
import DragScroll from '@/app/components/DragScroll';

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
  fx_rates: Record<string, number>;
}

const CCY_SYMBOL: Record<string, string> = { INR: '₹', USD: '$', GBP: '£', EUR: '€', SGD: 'S$', AED: 'AED ', HKD: 'HK$', CHF: 'CHF ' };

function fmtINR(n: number | null | undefined): string {
  if (n == null) return '—';
  return '₹' + Math.round(n).toLocaleString('en-IN');
}
function fmtNative(n: number | null | undefined, ccy: string): string {
  if (n == null) return '—';
  const sym = CCY_SYMBOL[ccy] || `${ccy} `;
  return sym + n.toLocaleString('en-IN', { maximumFractionDigits: 2 });
}
function fmtNum(n: number | null | undefined): string {
  if (n == null) return '—';
  return n.toLocaleString('en-IN', { maximumFractionDigits: 4 });
}
function fmtPct(n: number | null | undefined): string {
  if (n == null) return '—';
  return `${n >= 0 ? '+' : ''}${n.toFixed(2)}%`;
}
function gainClass(n: number | null | undefined): string {
  if (n == null || n === 0) return 'text-dim';
  return n > 0 ? 'text-gain' : 'text-loss';
}

function EntitySwitcher({
  entities, selectedId, onSelect,
}: { entities: Entity[]; selectedId: number | null; onSelect: (id: number | null) => void; }) {
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
              active ? 'bg-prime text-prime-fg' : 'bg-card border border-rule text-dim hover:border-dim hover:text-ink'
            }`}
          >
            {tab.name}
          </button>
        );
      })}
    </div>
  );
}

function HoldingsSection({
  title, subtitle, rows, total, showEntityCol, showNative,
}: {
  title: string; subtitle: string; rows: HoldingRow[]; total: number;
  showEntityCol: boolean; showNative: boolean;
}) {
  if (rows.length === 0) return null;
  return (
    <section className="bg-card rounded-lg border border-rule overflow-hidden mb-6">
      <div className="px-5 sm:px-6 pt-5 pb-4 border-b border-rule flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold text-ink">{title}</h2>
          <p className="text-xs text-ghost mt-0.5">{subtitle}</p>
        </div>
        <div className="text-right">
          <p className="text-[11px] uppercase tracking-wide text-ghost">Current Value</p>
          <p className="text-lg font-bold text-ink tabular-nums">{fmtINR(total)}</p>
        </div>
      </div>
      <DragScroll className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-ghost border-b border-rule">
              <th className="px-4 py-2.5 font-medium">Instrument</th>
              {showEntityCol && <th className="px-4 py-2.5 font-medium">Entity</th>}
              <th className="px-4 py-2.5 font-medium text-right">Qty</th>
              <th className="px-4 py-2.5 font-medium text-right">Invested (₹)</th>
              {showNative && <th className="px-4 py-2.5 font-medium text-right">Native Value</th>}
              <th className="px-4 py-2.5 font-medium text-right">Current Value (₹)</th>
              <th className="px-4 py-2.5 font-medium text-right">P&amp;L (₹)</th>
              <th className="px-4 py-2.5 font-medium text-right">Return</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(h => (
              <tr key={`${h.broker}-${h.entity_id}-${h.symbol}`} className="border-b border-rule last:border-0 hover:bg-page/60 transition-colors">
                <td className="px-4 py-3">
                  <div className="font-medium text-ink">{h.symbol}</div>
                  <div className="text-[11px] text-ghost">{h.sector || '—'}{h.currency !== 'INR' ? ` · ${h.currency}` : ''}</div>
                </td>
                {showEntityCol && <td className="px-4 py-3 text-dim">{h.entity_name}</td>}
                <td className="px-4 py-3 text-right tabular-nums text-dim">{fmtNum(h.quantity)}</td>
                <td className="px-4 py-3 text-right tabular-nums text-dim">{fmtINR(h.cost)}</td>
                {showNative && (
                  <td className="px-4 py-3 text-right tabular-nums text-dim">
                    {h.currency !== 'INR' ? fmtNative(h.current_market_value_native, h.currency) : '—'}
                  </td>
                )}
                <td className="px-4 py-3 text-right tabular-nums font-medium text-ink">{fmtINR(h.current_market_value)}</td>
                <td className={`px-4 py-3 text-right tabular-nums ${gainClass(h.pnl_inception)}`}>{fmtINR(h.pnl_inception)}</td>
                <td className={`px-4 py-3 text-right tabular-nums ${gainClass(h.returns_inception_pct)}`}>{fmtPct(h.returns_inception_pct)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </DragScroll>
    </section>
  );
}

export default function GoldSilverPage() {
  const router = useRouter();
  const [user, setUser]             = useState<User | null>(null);
  const [entities, setEntities]     = useState<Entity[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
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
    if (!didInitialLoad.current) setLoading(true);
    setError(null);
    const qs = selectedId !== null ? `?entity_id=${selectedId}` : '';
    fetch(`${API_URL}/api/v1/gold-silver/holdings${qs}`, { credentials: 'include', signal: controller.signal })
      .then(r => { if (r.status === 401) { router.push('/'); return null; } if (!r.ok) throw new Error('Failed to load gold/silver holdings.'); return r.json(); })
      .then((d: GoldSilverResponse | null) => {
        if (d) setData(d);
        setLoading(false);
        didInitialLoad.current = true;
      })
      .catch(err => { if (err.name !== 'AbortError') { setError(err.message); setLoading(false); } });
    return () => controller.abort();
  }, [router, selectedId, retryCount]);

  const isAdmin       = user?.role === 'admin';
  const showEntityCol = isAdmin && selectedId === null;
  const handleRetry   = useCallback(() => setRetryCount(c => c + 1), []);

  const grandTotal = data ? (data.metals_total + data.commodities_total) : 0;

  return (
    <main id="main-content" className="min-h-screen bg-page p-4 sm:p-8">
      <div className="max-w-screen-2xl mx-auto">

        <div className="flex flex-wrap justify-between items-start gap-3 mb-6">
          <div>
            <h1 className="text-2xl sm:text-3xl font-bold text-ink">Gold / Silver</h1>
            <span className="text-sm text-ghost">Precious metals (gold &amp; silver ETFs, sovereign gold bonds) and commodities</span>
          </div>
          <nav className="flex gap-1.5 flex-wrap" aria-label="Sections">
            {[
              { href: '/dashboard',      label: 'Overview'       },
              { href: '/mutual-funds',   label: 'Mutual Funds'   },
              { href: '/equity',         label: 'Equity'         },
              { href: '/foreign-equity', label: 'Foreign Equity' },
              { href: '/gold-silver',    label: 'Gold/Silver', active: true },
              { href: '/art', label: 'Art' },
              { href: '/properties', label: 'Properties' },
              { href: '/bank-accounts',  label: 'Banks'          },
              { href: '/pms',            label: 'PMS'            },
              { href: '/manual-data',    label: 'Manual Data'    },
              { href: '/reports',        label: 'Reports'        },
              { href: '/benchmarks',     label: 'Benchmarks'     },
              { href: '/realised-gains', label: 'Realised Gains' },
              { href: '/assistant',      label: 'Assistant'      },
            ].map(({ href, label, active }) => (
              <a
                key={href}
                href={href}
                aria-current={active ? 'page' : undefined}
                className={`px-3 py-1.5 rounded text-xs font-medium transition-colors ${
                  active ? 'bg-prime text-prime-fg' : 'bg-card border border-rule text-dim hover:border-dim hover:text-ink'
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

            <HoldingsSection
              title="Precious Metals"
              subtitle="Gold &amp; silver ETFs and sovereign gold bonds"
              rows={data.metals}
              total={data.metals_total}
              showEntityCol={showEntityCol}
              showNative={data.metals.some(h => h.currency !== 'INR')}
            />
            <HoldingsSection
              title="Commodities"
              subtitle="Uranium and other commodity instruments (incl. international)"
              rows={data.commodities}
              total={data.commodities_total}
              showEntityCol={showEntityCol}
              showNative={data.commodities.some(h => h.currency !== 'INR')}
            />
          </div>
        )}

        <p className="text-center text-xs text-ghost mt-8">
          IWS Finserv &copy; {new Date().getFullYear()} · Updates on each broker sync
        </p>
      </div>
    </main>
  );
}
