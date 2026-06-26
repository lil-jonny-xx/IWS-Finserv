'use client';
import { useEffect, useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import { useDragScroll } from '@/app/components/DragScroll';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

interface User { role: string; full_name: string; email?: string; entity_id?: number; }
interface Entity { id: number; name: string; }

interface PmsHolding {
  entity_id: number;
  entity_name: string;
  holding_type: 'equity' | 'cash';
  security_name: string;
  isin: string | null;
  quantity: number | null;
  avg_cost: number | null;
  cost: number | null;
  current_price: number | null;
  market_value: number;
  weight_pct: number | null;
}
interface PmsTotals {
  equity_total: number;
  cash_total: number;
  total: number;
  equity_cost: number;
  invested_cost: number;
  equity_pnl: number;
  equity_count: number;
  cash_count: number;
}
interface PmsByEntity {
  entity_id: number;
  entity_name: string;
  equity_cost: number;
  cash_total: number;
  equity_total: number;
  invested_cost: number;
  total: number;
}
interface PmsResponse {
  entity_id: number;
  entity_name: string;
  as_on_date: string | null;
  totals: PmsTotals;
  by_entity: PmsByEntity[];
  holdings: PmsHolding[];
}

const inr = (n: number | null | undefined) =>
  n == null ? '—' : n.toLocaleString('en-IN', { maximumFractionDigits: 0 });
const qty = (n: number | null | undefined) =>
  n == null ? '—' : n.toLocaleString('en-IN', { maximumFractionDigits: 2 });

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

function TotalCard({ label, value, sub, accent }: {
  label: string; value: string; sub?: string; accent?: 'gain' | 'loss';
}) {
  return (
    <div className="bg-card rounded-lg border border-rule px-5 py-4">
      <p className="text-xs text-ghost">{label}</p>
      <p className={`text-xl font-bold mt-1 ${accent ? `text-${accent}` : 'text-ink'}`}>{value}</p>
      {sub && <p className="text-xs text-ghost mt-0.5">{sub}</p>}
    </div>
  );
}

function InvestedByEntity({ rows }: { rows: PmsByEntity[] }) {
  const ds = useDragScroll();
  const grand = rows.reduce((s, r) => s + r.invested_cost, 0);
  return (
    <div ref={ds.ref} {...ds.bind} className="bg-card rounded-lg border border-rule overflow-x-auto mb-5">
      <table className="w-full min-w-[480px]">
        <thead>
          <tr className="text-left text-xs text-ghost">
            <th className="px-5 py-3 font-medium">Entity</th>
            <th className="px-5 py-3 font-medium text-right">Cost Invested</th>
            <th className="px-5 py-3 font-medium text-right">Cash</th>
            <th className="px-5 py-3 font-medium text-right">Total Invested</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(r => (
            <tr key={r.entity_id} className="border-t border-rule hover:bg-page">
              <td className="px-5 py-2.5 text-sm text-ink">{r.entity_name}</td>
              <td className="px-5 py-2.5 text-sm text-dim text-right tabular-nums">₹{inr(r.equity_cost)}</td>
              <td className="px-5 py-2.5 text-sm text-dim text-right tabular-nums">₹{inr(r.cash_total)}</td>
              <td className="px-5 py-2.5 text-sm text-ink font-medium text-right tabular-nums">₹{inr(r.invested_cost)}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr className="border-t border-rule bg-card font-semibold">
            <td className="px-5 py-2.5 text-xs text-dim uppercase tracking-wide">All entities</td>
            <td className="px-5 py-2.5 text-sm text-dim text-right tabular-nums">₹{inr(rows.reduce((s, r) => s + r.equity_cost, 0))}</td>
            <td className="px-5 py-2.5 text-sm text-dim text-right tabular-nums">₹{inr(rows.reduce((s, r) => s + r.cash_total, 0))}</td>
            <td className="px-5 py-2.5 text-sm text-ink text-right tabular-nums">₹{inr(grand)}</td>
          </tr>
        </tfoot>
      </table>
    </div>
  );
}

function HoldingsTable({ rows, showEntityCol }: { rows: PmsHolding[]; showEntityCol: boolean }) {
  const ds = useDragScroll();
  const equity = rows.filter(r => r.holding_type === 'equity');
  const cash   = rows.filter(r => r.holding_type === 'cash');

  const section = (title: string, items: PmsHolding[], subtotal: number) => (
    items.length > 0 && (
      <>
        <tr className="bg-page">
          <td colSpan={showEntityCol ? 7 : 6} className="px-5 py-2 text-xs font-semibold text-dim uppercase tracking-wide">
            {title}
          </td>
        </tr>
        {items.map((h, i) => (
          <tr key={`${title}-${i}`} className="border-t border-rule hover:bg-page">
            {showEntityCol && <td className="px-5 py-2.5 text-xs text-dim">{h.entity_name}</td>}
            <td className="px-5 py-2.5 text-sm text-ink">{h.security_name}</td>
            <td className="px-5 py-2.5 text-xs text-ghost">{h.isin ?? '—'}</td>
            <td className="px-5 py-2.5 text-sm text-dim text-right tabular-nums">{qty(h.quantity)}</td>
            <td className="px-5 py-2.5 text-sm text-dim text-right tabular-nums">{inr(h.cost)}</td>
            <td className="px-5 py-2.5 text-sm text-dim text-right tabular-nums">{inr(h.market_value)}</td>
            <td className="px-5 py-2.5 text-sm text-right tabular-nums text-dim">
              {h.weight_pct == null ? '—' : `${h.weight_pct.toFixed(1)}%`}
            </td>
          </tr>
        ))}
        <tr className="border-t border-rule bg-card font-medium">
          <td colSpan={showEntityCol ? 5 : 4} className="px-5 py-2.5 text-xs text-dim">{title} total</td>
          <td className="px-5 py-2.5 text-sm text-ink text-right tabular-nums">{inr(subtotal)}</td>
          <td />
        </tr>
      </>
    )
  );

  return (
    <div ref={ds.ref} {...ds.bind} className="bg-card rounded-lg border border-rule overflow-x-auto">
      <table className="w-full min-w-[640px]">
        <thead>
          <tr className="text-left text-xs text-ghost">
            {showEntityCol && <th className="px-5 py-3 font-medium">Entity</th>}
            <th className="px-5 py-3 font-medium">Security</th>
            <th className="px-5 py-3 font-medium">ISIN</th>
            <th className="px-5 py-3 font-medium text-right">Qty</th>
            <th className="px-5 py-3 font-medium text-right">Cost</th>
            <th className="px-5 py-3 font-medium text-right">Market Value</th>
            <th className="px-5 py-3 font-medium text-right">Weight</th>
          </tr>
        </thead>
        <tbody>
          {section('Equity', equity, equity.reduce((s, h) => s + h.market_value, 0))}
          {section('Cash', cash, cash.reduce((s, h) => s + h.market_value, 0))}
        </tbody>
      </table>
    </div>
  );
}

export default function PmsPage() {
  const router = useRouter();
  const [user, setUser]             = useState<User | null>(null);
  const [entities, setEntities]     = useState<Entity[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [data, setData]             = useState<PmsResponse | null>(null);
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
    fetch(`${API_URL}/api/v1/pms/holdings${qs}`, { credentials: 'include', signal: controller.signal })
      .then(r => { if (r.status === 401) { router.push('/'); return null; } if (!r.ok) throw new Error('Failed to load PMS holdings.'); return r.json(); })
      .then((d: PmsResponse | null) => { if (d) setData(d); setLoading(false); })
      .catch(err => { if (err.name !== 'AbortError') { setError(err.message); setLoading(false); } });
    return () => controller.abort();
  }, [router, selectedId, retryCount]);

  const isAdmin       = user?.role === 'admin';
  const showEntityCol = isAdmin && selectedId === null;
  const handleRetry   = useCallback(() => setRetryCount(c => c + 1), []);
  const t = data?.totals;

  return (
    <main id="main-content" className="min-h-screen bg-page p-4 sm:p-8">
      <div className="max-w-screen-2xl mx-auto">

        <div className="flex flex-wrap justify-between items-start gap-3 mb-6">
          <div>
            <h1 className="text-2xl sm:text-3xl font-bold text-ink">PMS Portfolio</h1>
            <p className="text-sm text-ghost mt-0.5">
              PMS holdings{data?.as_on_date ? ` · as on ${data.as_on_date}` : ''}
            </p>
          </div>
          <nav className="flex gap-1.5" aria-label="Sections">
            {[
              { href: '/dashboard',     label: 'Overview'       },
              { href: '/mutual-funds',  label: 'Mutual Funds'   },
              { href: '/equity',        label: 'Equity'         },
              { href: '/foreign-equity', label: 'Foreign Equity' },
              { href: '/gold-silver', label: 'Gold/Silver' },
              { href: '/art', label: 'Art' },
              { href: '/properties', label: 'Properties' },
              { href: '/bank-accounts', label: 'Banks' },
              { href: '/pms',           label: 'PMS', active: true },
              { href: '/manual-data',   label: 'Manual Data'    },
              { href: '/reports',       label: 'Reports'        },
              { href: '/benchmarks',    label: 'Benchmarks'     },
              { href: '/realised-gains', label: 'Realised Gains' },
              { href: '/assistant',     label: 'Assistant'      },
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

        {loading && !data && (
          <div className="bg-card rounded-lg border border-rule px-5 py-10 text-center text-sm text-ghost animate-pulse">
            Loading PMS holdings…
          </div>
        )}

        {error && !data && (
          <div role="alert" className="bg-card rounded-lg border border-rule px-5 py-5 flex items-start justify-between gap-4">
            <div>
              <p className="text-sm font-medium text-dim">Could not load PMS holdings</p>
              <p className="text-xs text-ghost mt-1">{error}</p>
            </div>
            <button onClick={handleRetry} className="shrink-0 text-xs border border-wire text-dim px-3 py-1.5 rounded hover:border-dim hover:text-ink transition-colors">
              Retry
            </button>
          </div>
        )}

        {data && t && (
          <>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 mb-5">
              <TotalCard label="Total Invested (Cost + Cash)" value={`₹${inr(t.invested_cost)}`}
                sub={`Cost ₹${inr(t.equity_cost)} + Cash ₹${inr(t.cash_total)}`} />
              <TotalCard label="Equity" value={`₹${inr(t.equity_total)}`}
                sub={`${t.equity_count} holding${t.equity_count === 1 ? '' : 's'} · P&L ₹${inr(t.equity_pnl)}`}
                accent={t.equity_pnl >= 0 ? 'gain' : 'loss'} />
              <TotalCard label="Cash" value={`₹${inr(t.cash_total)}`}
                sub={`${t.cash_count} account${t.cash_count === 1 ? '' : 's'}`} />
              <TotalCard label="Total (Equity + Cash)" value={`₹${inr(t.total)}`} />
            </div>

            {showEntityCol && data.by_entity.length > 1 && (
              <InvestedByEntity rows={data.by_entity} />
            )}

            {data.holdings.length === 0 ? (
              <div className="bg-card rounded-lg border border-rule px-5 py-10 text-center text-sm text-ghost">
                No PMS holdings yet. They appear here once the Nuvama report has been synced.
              </div>
            ) : (
              <HoldingsTable rows={data.holdings} showEntityCol={showEntityCol} />
            )}
          </>
        )}

        <p className="text-center text-xs text-ghost mt-8">
          IWS Finserv &copy; {new Date().getFullYear()} · PMS data sourced from the Nuvama WealthSpectrum report
        </p>
      </div>
    </main>
  );
}
