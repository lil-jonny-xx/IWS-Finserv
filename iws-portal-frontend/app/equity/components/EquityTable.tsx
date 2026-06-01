'use client';
import { useState, Fragment } from 'react';

export interface EquityHoldingRow {
  id: number;
  entity_id: number;
  entity_name?: string;
  broker: string;
  symbol: string;
  isin?: string;
  exchange?: string;
  quantity: number;
  avg_cost: number;
  cost: number;
  current_price?: number;
  current_market_value?: number;
  prev_week_value?: number;
  market_value_as_on?: number;
  as_of_date?: string;
  exposure_pct?: number;
  weekly_change?: number;
  pnl_ytd?: number;
  pnl_inception?: number;
  pnl_weekly_change?: number;
  returns_ytd_pct?: number;
  returns_inception_pct?: number;
  cagr_inception_pct?: number;
  first_invested_date?: string;
  remarks?: string;
}

export interface EquityTotals {
  total_cost?: number;
  total_current_market_value?: number;
  total_pnl_inception?: number;
  total_pnl_ytd?: number;
  total_weekly_change?: number;
  grand_total?: number;
}

interface Props {
  holdings: EquityHoldingRow[];
  totals: EquityTotals;
  showEntityCol: boolean;
}

function fmtINR(n: number | null | undefined): string {
  if (n == null) return '—';
  const abs = Math.round(Math.abs(n));
  const str = abs.toString();
  const last3 = str.slice(-3);
  const rest = str.slice(0, -3);
  const grouped = rest.length > 0
    ? rest.replace(/\B(?=(\d{2})+(?!\d))/g, ',') + ',' + last3
    : last3;
  return (n < 0 ? '−₹' : '₹') + grouped;
}

function fmtPct(n: number | null | undefined, decimals = 2): string {
  if (n == null) return '—';
  return (n >= 0 ? '+' : '') + n.toFixed(decimals) + '%';
}

function fmtDate(iso: string | undefined): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' });
}

function ColorNum({ n, fmt }: { n: number | null | undefined; fmt: (n: number) => string }) {
  if (n == null) return <span className="text-ghost">—</span>;
  return (
    <span style={{ color: n >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
      {fmt(n)}
    </span>
  );
}

const BROKER_LABELS: Record<string, string> = {
  zerodha: 'Zerodha',
  angel_one: 'Angel One',
  dhan: 'Dhan',
};

type SortKey = keyof EquityHoldingRow;
type SortDir = 'asc' | 'desc';

function sorted(rows: EquityHoldingRow[], key: SortKey, dir: SortDir): EquityHoldingRow[] {
  return [...rows].sort((a, b) => {
    const va = a[key] ?? (typeof a[key] === 'number' ? -Infinity : '');
    const vb = b[key] ?? (typeof b[key] === 'number' ? -Infinity : '');
    if (va < vb) return dir === 'asc' ? -1 : 1;
    if (va > vb) return dir === 'asc' ? 1 : -1;
    return 0;
  });
}

function SortArrow({ col, sortKey, sortDir }: { col: SortKey; sortKey: SortKey; sortDir: SortDir }) {
  const active = col === sortKey;
  return (
    <span aria-hidden className={`ml-1 text-[9px] ${active ? 'text-prime' : 'opacity-30'}`}>
      {active ? (sortDir === 'asc' ? '↑' : '↓') : '↕'}
    </span>
  );
}

// First row of the multi-level header — defines all column groups
function HeaderRow1({
  showEntityCol,
  sortKey,
  sortDir,
  onSort,
}: {
  showEntityCol: boolean;
  sortKey: SortKey;
  sortDir: SortDir;
  onSort: (k: SortKey) => void;
}) {
  const thBase = 'px-3 py-2.5 text-xs font-medium text-ghost bg-card border-b border-rule whitespace-nowrap';
  const thR = thBase + ' text-right';
  const thL = thBase + ' text-left';

  function SortTh({
    col, label, right = true, rowSpan = 1, className = '',
  }: {
    col: SortKey; label: string; right?: boolean; rowSpan?: number; className?: string;
  }) {
    return (
      <th
        scope="col"
        rowSpan={rowSpan}
        className={`${right ? thR : thL} ${className}`}
        aria-sort={sortKey === col ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}
      >
        <button
          onClick={() => onSort(col)}
          className={`inline-flex items-center hover:text-ink transition-colors ${right ? 'ml-auto' : ''}`}
        >
          {label}<SortArrow col={col} sortKey={sortKey} sortDir={sortDir} />
        </button>
      </th>
    );
  }

  return (
    <tr>
      {/* fixed identity columns */}
      <SortTh col="symbol"   label="Symbol"   right={false} rowSpan={2} className="pl-5 sm:pl-6 sticky left-0 z-20 bg-card" />
      {showEntityCol && (
        <SortTh col="entity_name" label="Entity" right={false} rowSpan={2} />
      )}
      <SortTh col="broker"   label="Broker"   right={false} rowSpan={2} />
      <SortTh col="exchange" label="Exch"     right={false} rowSpan={2} />
      <SortTh col="quantity" label="Qty"      rowSpan={2} />
      <SortTh col="cost"     label="Cost"     rowSpan={2} />

      {/* single-col value headers */}
      <SortTh col="current_market_value" label="Mkt Value" rowSpan={2} />
      <SortTh col="prev_week_value"      label="Prev Week"  rowSpan={2} />
      <SortTh col="weekly_change"        label="Wkly Chg"  rowSpan={2} />
      <SortTh col="exposure_pct"         label="Exp %"     rowSpan={2} />

      {/* P&L group */}
      <th colSpan={3} className={`${thBase} text-center border-l border-rule`}>
        P&amp;L
      </th>

      {/* Returns group */}
      <th colSpan={3} className={`${thBase} text-center border-l border-rule`}>
        Returns
      </th>

      <th scope="col" rowSpan={2} className={`${thL} pr-5 sm:pr-6`}>Remarks</th>
    </tr>
  );
}

function HeaderRow2({
  sortKey, sortDir, onSort,
}: {
  sortKey: SortKey; sortDir: SortDir; onSort: (k: SortKey) => void;
}) {
  const thBase = 'px-3 py-2 text-xs font-medium text-ghost bg-card border-b border-rule whitespace-nowrap text-right';

  function SortTh({ col, label, borderL = false }: { col: SortKey; label: string; borderL?: boolean }) {
    return (
      <th
        scope="col"
        className={`${thBase} ${borderL ? 'border-l border-rule' : ''}`}
        aria-sort={sortKey === col ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}
      >
        <button
          onClick={() => onSort(col)}
          className="inline-flex items-center ml-auto hover:text-ink transition-colors"
        >
          {label}<SortArrow col={col} sortKey={sortKey} sortDir={sortDir} />
        </button>
      </th>
    );
  }

  return (
    <tr>
      <SortTh col="pnl_ytd"             label="YTD"     borderL />
      <SortTh col="pnl_inception"        label="Inception" />
      <SortTh col="pnl_weekly_change"    label="Wkly Chg" />
      <SortTh col="returns_ytd_pct"      label="YTD %"   borderL />
      <SortTh col="returns_inception_pct" label="Inc %" />
      <SortTh col="cagr_inception_pct"   label="CAGR" />
    </tr>
  );
}

export default function EquityTable({ holdings, totals, showEntityCol }: Props) {
  const [sortKey, setSortKey] = useState<SortKey>('current_market_value');
  const [sortDir, setSortDir] = useState<SortDir>('desc');
  const [search, setSearch] = useState('');

  if (holdings.length === 0) {
    return (
      <div className="bg-card rounded-lg border border-rule px-6 py-12 text-center">
        <p className="text-sm font-medium text-ink mb-1">No equity holdings on record</p>
        <p className="text-xs text-ghost">Holdings will appear after the first broker sync.</p>
      </div>
    );
  }

  function handleSort(key: SortKey) {
    if (key === sortKey) setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    else { setSortKey(key); setSortDir('desc'); }
  }

  const q = search.toLowerCase();
  const filtered = q
    ? holdings.filter(h =>
        h.symbol.toLowerCase().includes(q) ||
        (h.entity_name ?? '').toLowerCase().includes(q) ||
        BROKER_LABELS[h.broker]?.toLowerCase().includes(q) ||
        (h.isin ?? '').toLowerCase().includes(q)
      )
    : holdings;

  const rows = sorted(filtered, sortKey, sortDir);

  // Group by broker for sub-headers
  const brokers = [...new Set(rows.map(h => h.broker))];
  const byBroker: Record<string, EquityHoldingRow[]> = {};
  for (const b of brokers) byBroker[b] = rows.filter(h => h.broker === b);

  const asOfDate = holdings.find(h => h.as_of_date)?.as_of_date;

  const colCount = (showEntityCol ? 1 : 0) + 14; // base + optional entity col

  return (
    <div className="bg-card rounded-lg border border-rule overflow-hidden">

      {/* Summary strip */}
      <div className="px-5 sm:px-6 pt-5 pb-4 border-b border-rule">
        <div className="flex items-start justify-between gap-3 mb-3">
          <h2 className="text-base font-semibold text-ink">
            Equity Holdings
            {asOfDate && (
              <span className="ml-3 text-xs font-normal text-ghost">
                as of {fmtDate(asOfDate)}
              </span>
            )}
          </h2>
        </div>
        <div className="flex flex-wrap gap-x-8 gap-y-3">
          {totals.total_cost != null && (
            <div>
              <p className="text-xs text-ghost mb-0.5">Total Cost</p>
              <p className="text-sm font-semibold text-ink tabular-nums">{fmtINR(totals.total_cost)}</p>
            </div>
          )}
          {totals.total_current_market_value != null && (
            <div>
              <p className="text-xs text-ghost mb-0.5">Market Value</p>
              <p className="text-sm font-semibold text-ink tabular-nums">{fmtINR(totals.total_current_market_value)}</p>
            </div>
          )}
          {totals.total_pnl_inception != null && (
            <div>
              <p className="text-xs text-ghost mb-0.5">P&amp;L (Inception)</p>
              <p className="text-sm font-semibold tabular-nums"
                style={{ color: totals.total_pnl_inception >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
                {totals.total_pnl_inception >= 0 ? '+' : ''}
                {fmtINR(totals.total_pnl_inception)}
              </p>
            </div>
          )}
          {totals.total_pnl_ytd != null && (
            <div>
              <p className="text-xs text-ghost mb-0.5">P&amp;L YTD</p>
              <p className="text-sm font-semibold tabular-nums"
                style={{ color: totals.total_pnl_ytd >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
                {totals.total_pnl_ytd >= 0 ? '+' : ''}
                {fmtINR(totals.total_pnl_ytd)}
              </p>
            </div>
          )}
          {totals.total_weekly_change != null && (
            <div>
              <p className="text-xs text-ghost mb-0.5">Weekly Change</p>
              <p className="text-sm font-semibold tabular-nums"
                style={{ color: totals.total_weekly_change >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
                {totals.total_weekly_change >= 0 ? '+' : ''}
                {fmtINR(totals.total_weekly_change)}
              </p>
            </div>
          )}
          <div>
            <p className="text-xs text-ghost mb-0.5">Holdings</p>
            <p className="text-sm font-semibold text-ink tabular-nums">{holdings.length}</p>
          </div>
        </div>
      </div>

      {/* Search */}
      <div className="px-5 sm:px-6 py-3 border-b border-rule">
        <input
          type="search"
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder="Search symbol, broker or entity…"
          aria-label="Search equity holdings"
          className="w-full max-w-sm text-xs bg-page border border-wire rounded px-3 py-1.5 text-ink placeholder:text-ghost focus:outline-none focus:border-prime transition-colors"
        />
      </div>

      {/* Table */}
      <div className="overflow-x-auto" role="region" aria-label="Equity holdings table" tabIndex={0}>
        <table className="w-full text-sm" style={{ minWidth: '1400px' }}>
          <thead>
            <HeaderRow1
              showEntityCol={showEntityCol}
              sortKey={sortKey}
              sortDir={sortDir}
              onSort={handleSort}
            />
            <HeaderRow2
              sortKey={sortKey}
              sortDir={sortDir}
              onSort={handleSort}
            />
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={colCount} className="px-5 sm:px-6 py-8 text-center text-xs text-ghost">
                  No holdings match your search.
                </td>
              </tr>
            ) : (
              brokers.map(broker => {
                const group = byBroker[broker];
                if (!group?.length) return null;
                const subtotal = group.reduce((s, h) => s + (h.current_market_value ?? 0), 0);
                return (
                  <Fragment key={broker}>
                    <tr>
                      <td colSpan={colCount} className="px-5 sm:px-6 py-2 bg-page">
                        <span className="text-xs font-semibold text-dim">
                          {BROKER_LABELS[broker] ?? broker}
                        </span>
                        <span className="ml-2 text-xs text-ghost">{fmtINR(subtotal)}</span>
                      </td>
                    </tr>
                    {group.map(h => (
                      <tr key={h.id} className="border-t border-rule hover:bg-page transition-colors duration-100">
                        <td className="px-3 pl-5 sm:pl-6 py-3 align-top sticky left-0 bg-card hover:bg-page whitespace-nowrap">
                          <p className="text-xs font-medium text-ink">{h.symbol}</p>
                          {h.isin && <p className="text-[10px] text-ghost font-mono mt-0.5">{h.isin}</p>}
                        </td>
                        {showEntityCol && (
                          <td className="px-3 py-3 text-xs font-medium text-dim whitespace-nowrap align-top">
                            {h.entity_name ?? '—'}
                          </td>
                        )}
                        <td className="px-3 py-3 text-xs text-dim whitespace-nowrap align-top">
                          {BROKER_LABELS[h.broker] ?? h.broker}
                        </td>
                        <td className="px-3 py-3 text-xs text-ghost whitespace-nowrap align-top">
                          {h.exchange ?? '—'}
                        </td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs text-ink whitespace-nowrap align-top">
                          {h.quantity.toFixed(0)}
                        </td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs text-ink whitespace-nowrap align-top">
                          {fmtINR(h.cost)}
                        </td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs text-ink whitespace-nowrap align-top">
                          {fmtINR(h.current_market_value)}
                        </td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs text-dim whitespace-nowrap align-top">
                          {fmtINR(h.prev_week_value)}
                        </td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top">
                          <ColorNum n={h.weekly_change} fmt={fmtINR} />
                        </td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs text-dim whitespace-nowrap align-top">
                          {h.exposure_pct != null ? h.exposure_pct.toFixed(2) + '%' : '—'}
                        </td>
                        {/* P&L group */}
                        <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top border-l border-rule">
                          <ColorNum n={h.pnl_ytd} fmt={fmtINR} />
                        </td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top">
                          <ColorNum n={h.pnl_inception} fmt={fmtINR} />
                        </td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top">
                          <ColorNum n={h.pnl_weekly_change} fmt={fmtINR} />
                        </td>
                        {/* Returns group */}
                        <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top border-l border-rule">
                          <ColorNum n={h.returns_ytd_pct} fmt={n => fmtPct(n)} />
                        </td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top">
                          <ColorNum n={h.returns_inception_pct} fmt={n => fmtPct(n)} />
                        </td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top">
                          {h.cagr_inception_pct != null ? (
                            <span style={{ color: h.cagr_inception_pct >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
                              {fmtPct(h.cagr_inception_pct)} p.a.
                            </span>
                          ) : <span className="text-ghost">—</span>}
                        </td>
                        <td className="px-3 pr-5 sm:pr-6 py-3 text-xs text-ghost align-top max-w-[160px]">
                          {h.remarks ?? '—'}
                        </td>
                      </tr>
                    ))}
                  </Fragment>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
