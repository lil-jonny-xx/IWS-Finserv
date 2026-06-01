'use client';
import { useState, Fragment } from 'react';

export interface MFHoldingRow {
  id: number;
  isin: string;
  security_name: string;
  security_type: string;
  asset_class: string;
  amfi_code?: string;
  folio_number: string;
  quantity: number;
  avg_cost?: number;
  cost_basis?: number;
  invested_amount: number;
  nav?: number;
  current_value?: number;
  first_invested_date?: string;
  last_updated?: string;
  entity_name?: string;
  // metric columns (written by mf_metrics_worker)
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
  remarks?: string;
}

export interface MFTotals {
  total_holdings?: number;
  total_invested?: number;
  total_current_value?: number;
}

interface Props {
  holdings: MFHoldingRow[];
  totals: MFTotals;
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

function fmtPct(n: number | null | undefined): string {
  if (n == null) return '—';
  return (n >= 0 ? '+' : '') + n.toFixed(2) + '%';
}

function fmtDate(iso: string | undefined): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' });
}

function ColorNum({ n, fmt }: { n: number | null | undefined; fmt: (v: number) => string }) {
  if (n == null) return <span className="text-ghost">—</span>;
  return (
    <span style={{ color: n >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
      {fmt(n)}
    </span>
  );
}

const ASSET_CLASS_ORDER = ['EQUITY', 'FIXED_INCOME', 'ALTERNATES'] as const;
const ASSET_CLASS_LABELS: Record<string, string> = {
  EQUITY:       'Equity',
  FIXED_INCOME: 'Fixed Income',
  ALTERNATES:   'Alternates',
};
const SEC_TYPE_LABELS: Record<string, string> = {
  MF_EQUITY:    'Equity Fund',
  MF_DEBT:      'Debt Fund',
  MF_HYBRID:    'Hybrid Fund',
  MF_LIQUID:    'Liquid Fund',
  MF_FOF:       'Fund of Funds',
  MF_ELSS:      'ELSS',
  MF_ETF:       'Index ETF',
  MF_ARBITRAGE: 'Arbitrage Fund',
  GOLD_ETF:     'Gold ETF',
  MF_OTHER:     'Other MF',
};

type SortKey = keyof MFHoldingRow;
type SortDir = 'asc' | 'desc';

function sortRows(rows: MFHoldingRow[], key: SortKey, dir: SortDir): MFHoldingRow[] {
  return [...rows].sort((a, b) => {
    const va = (a[key] as number | string) ?? (typeof a[key] === 'number' ? -Infinity : '');
    const vb = (b[key] as number | string) ?? (typeof b[key] === 'number' ? -Infinity : '');
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

function HeaderRow1({
  showEntityCol, sortKey, sortDir, onSort,
}: {
  showEntityCol: boolean; sortKey: SortKey; sortDir: SortDir; onSort: (k: SortKey) => void;
}) {
  const base = 'px-3 py-2.5 text-xs font-medium text-ghost bg-card border-b border-rule whitespace-nowrap';

  function SortTh({ col, label, right = true, rowSpan = 1, first = false }: {
    col: SortKey; label: string; right?: boolean; rowSpan?: number; first?: boolean;
  }) {
    return (
      <th
        scope="col"
        rowSpan={rowSpan}
        className={`${base} ${right ? 'text-right' : 'text-left'} ${first ? 'pl-5 sm:pl-6' : ''}`}
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
      <SortTh col="security_name" label="Fund"     right={false} rowSpan={2} first />
      {showEntityCol && <SortTh col="entity_name"  label="Entity"  right={false} rowSpan={2} />}
      <SortTh col="folio_number"  label="Folio"    right={false} rowSpan={2} />
      <SortTh col="quantity"      label="Units"    rowSpan={2} />
      <SortTh col="invested_amount" label="Cost"   rowSpan={2} />
      <SortTh col="exposure_pct"  label="Exp %"    rowSpan={2} />
      <SortTh col="market_value_as_on" label="Mkt Value" rowSpan={2} />
      <SortTh col="prev_week_value"    label="Prev Week" rowSpan={2} />
      <SortTh col="weekly_change"      label="Wkly Chg"  rowSpan={2} />
      {/* P&L group */}
      <th colSpan={3} className={`${base} text-center border-l border-rule`}>P&amp;L</th>
      {/* Returns group */}
      <th colSpan={3} className={`${base} text-center border-l border-rule`}>Returns</th>
      <th scope="col" rowSpan={2} className={`${base} text-left pr-5 sm:pr-6`}>Remarks</th>
    </tr>
  );
}

function HeaderRow2({ sortKey, sortDir, onSort }: {
  sortKey: SortKey; sortDir: SortDir; onSort: (k: SortKey) => void;
}) {
  const base = 'px-3 py-2 text-xs font-medium text-ghost bg-card border-b border-rule whitespace-nowrap text-right';

  function SortTh({ col, label, borderL = false }: { col: SortKey; label: string; borderL?: boolean }) {
    return (
      <th
        scope="col"
        className={`${base} ${borderL ? 'border-l border-rule' : ''}`}
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
      <SortTh col="pnl_ytd"             label="YTD"      borderL />
      <SortTh col="pnl_inception"        label="Inception" />
      <SortTh col="pnl_weekly_change"    label="Wkly Chg" />
      <SortTh col="returns_ytd_pct"      label="YTD %"   borderL />
      <SortTh col="returns_inception_pct" label="Inc %" />
      <SortTh col="cagr_inception_pct"   label="CAGR" />
    </tr>
  );
}

export default function MFTable({ holdings, totals, showEntityCol }: Props) {
  const [sortKey, setSortKey]   = useState<SortKey>('market_value_as_on');
  const [sortDir, setSortDir]   = useState<SortDir>('desc');
  const [search, setSearch]     = useState('');

  if (holdings.length === 0) {
    return (
      <div className="bg-card rounded-lg border border-rule px-6 py-12 text-center">
        <p className="text-sm font-medium text-ink mb-1">No mutual fund holdings on record</p>
        <p className="text-xs text-ghost">Holdings will appear once your CAS statement has been imported.</p>
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
        h.security_name.toLowerCase().includes(q) ||
        h.folio_number.toLowerCase().includes(q) ||
        (h.entity_name ?? '').toLowerCase().includes(q)
      )
    : holdings;

  const asOfDate = holdings.find(h => h.as_of_date)?.as_of_date;

  const grouped: Record<string, MFHoldingRow[]> = Object.fromEntries(
    ASSET_CLASS_ORDER.map(cls => [
      cls,
      sortRows(filtered.filter(h => h.asset_class === cls), sortKey, sortDir),
    ])
  );
  const activeClasses = ASSET_CLASS_ORDER.filter(cls => (grouped[cls]?.length ?? 0) > 0);

  const colCount = (showEntityCol ? 1 : 0) + 15;

  const totalPnlInception = holdings.reduce((s, h) => s + (h.pnl_inception ?? 0), 0);
  const totalPnlYtd       = holdings.reduce((s, h) => s + (h.pnl_ytd ?? 0), 0);
  const totalWeeklyChg    = holdings.reduce((s, h) => s + (h.weekly_change ?? 0), 0);
  const hasMath           = holdings.some(h => h.pnl_inception != null);

  return (
    <div className="bg-card rounded-lg border border-rule overflow-hidden">

      {/* Summary strip */}
      <div className="px-5 sm:px-6 pt-5 pb-4 border-b border-rule">
        <div className="flex items-start justify-between gap-3 mb-3">
          <h2 className="text-base font-semibold text-ink">
            Mutual Fund Holdings
            {asOfDate && (
              <span className="ml-3 text-xs font-normal text-ghost">as of {fmtDate(asOfDate)}</span>
            )}
          </h2>
        </div>
        <div className="flex flex-wrap gap-x-8 gap-y-3">
          {totals.total_invested != null && (
            <div>
              <p className="text-xs text-ghost mb-0.5">Total Invested</p>
              <p className="text-sm font-semibold text-ink tabular-nums">{fmtINR(totals.total_invested)}</p>
            </div>
          )}
          {totals.total_current_value != null && (
            <div>
              <p className="text-xs text-ghost mb-0.5">Market Value</p>
              <p className="text-sm font-semibold text-ink tabular-nums">{fmtINR(totals.total_current_value)}</p>
            </div>
          )}
          {hasMath && (
            <>
              <div>
                <p className="text-xs text-ghost mb-0.5">P&amp;L (Inception)</p>
                <p className="text-sm font-semibold tabular-nums"
                  style={{ color: totalPnlInception >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
                  {totalPnlInception >= 0 ? '+' : ''}{fmtINR(totalPnlInception)}
                </p>
              </div>
              <div>
                <p className="text-xs text-ghost mb-0.5">P&amp;L YTD</p>
                <p className="text-sm font-semibold tabular-nums"
                  style={{ color: totalPnlYtd >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
                  {totalPnlYtd >= 0 ? '+' : ''}{fmtINR(totalPnlYtd)}
                </p>
              </div>
              <div>
                <p className="text-xs text-ghost mb-0.5">Weekly Change</p>
                <p className="text-sm font-semibold tabular-nums"
                  style={{ color: totalWeeklyChg >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
                  {totalWeeklyChg >= 0 ? '+' : ''}{fmtINR(totalWeeklyChg)}
                </p>
              </div>
            </>
          )}
          {totals.total_holdings != null && (
            <div>
              <p className="text-xs text-ghost mb-0.5">Holdings</p>
              <p className="text-sm font-semibold text-ink tabular-nums">{totals.total_holdings}</p>
            </div>
          )}
        </div>
      </div>

      {/* Search */}
      <div className="px-5 sm:px-6 py-3 border-b border-rule">
        <input
          type="search"
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder="Search fund, folio or entity…"
          aria-label="Search mutual fund holdings"
          className="w-full max-w-sm text-xs bg-page border border-wire rounded px-3 py-1.5 text-ink placeholder:text-ghost focus:outline-none focus:border-prime transition-colors"
        />
      </div>

      {/* Table */}
      <div className="overflow-x-auto" role="region" aria-label="MF holdings table" tabIndex={0}>
        <table className="w-full text-sm" style={{ minWidth: '1500px' }}>
          <thead>
            <HeaderRow1
              showEntityCol={showEntityCol}
              sortKey={sortKey}
              sortDir={sortDir}
              onSort={handleSort}
            />
            <HeaderRow2 sortKey={sortKey} sortDir={sortDir} onSort={handleSort} />
          </thead>
          <tbody>
            {activeClasses.length === 0 ? (
              <tr>
                <td colSpan={colCount} className="px-5 sm:px-6 py-8 text-center text-xs text-ghost">
                  No holdings match your search.
                </td>
              </tr>
            ) : activeClasses.map(cls => {
              const group = grouped[cls];
              if (!group?.length) return null;
              const subtotal = group.reduce((s, h) => s + (h.market_value_as_on ?? h.current_value ?? 0), 0);

              return (
                <Fragment key={cls}>
                  <tr>
                    <td colSpan={colCount} className="px-5 sm:px-6 py-2 bg-page">
                      <span className="text-xs font-semibold text-dim">
                        {ASSET_CLASS_LABELS[cls] ?? cls}
                      </span>
                      <span className="ml-2 text-xs text-ghost">{fmtINR(subtotal)}</span>
                    </td>
                  </tr>
                  {group.map(h => {
                    const mktVal = h.market_value_as_on ?? h.current_value;
                    return (
                      <tr key={h.id} className="border-t border-rule hover:bg-page transition-colors duration-100">
                        <td className="px-3 pl-5 sm:pl-6 py-3 align-top">
                          <p className="text-xs text-ink leading-snug">{h.security_name}</p>
                          <p className="text-[10px] text-ghost mt-0.5">
                            {SEC_TYPE_LABELS[h.security_type] ?? h.security_type}
                          </p>
                        </td>
                        {showEntityCol && (
                          <td className="px-3 py-3 text-xs font-medium text-dim whitespace-nowrap align-top">
                            {h.entity_name ?? '—'}
                          </td>
                        )}
                        <td className="px-3 py-3 font-mono text-xs text-dim whitespace-nowrap align-top">
                          {h.folio_number}
                        </td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs text-ink whitespace-nowrap align-top">
                          {h.quantity.toFixed(3)}
                        </td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs text-ink whitespace-nowrap align-top">
                          {fmtINR(h.invested_amount)}
                        </td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs text-dim whitespace-nowrap align-top">
                          {h.exposure_pct != null ? h.exposure_pct.toFixed(2) + '%' : '—'}
                        </td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs text-ink whitespace-nowrap align-top">
                          {fmtINR(mktVal)}
                        </td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs text-dim whitespace-nowrap align-top">
                          {fmtINR(h.prev_week_value)}
                        </td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top">
                          <ColorNum n={h.weekly_change} fmt={fmtINR} />
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
                          <ColorNum n={h.returns_ytd_pct} fmt={fmtPct} />
                        </td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top">
                          <ColorNum n={h.returns_inception_pct} fmt={fmtPct} />
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
                    );
                  })}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
