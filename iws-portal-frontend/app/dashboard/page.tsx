'use client';
import { useEffect, useState, useCallback, Fragment } from 'react';
import { useRouter } from 'next/navigation';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';
const IDLE_TIMEOUT = 30 * 60 * 1000;

interface User {
  full_name: string;
  email: string;
  role: string;
  entity_id: string | number;
}

interface Entity {
  id: number;
  name: string;
  pan_group: string;
}

interface Holding {
  id: number;
  isin: string;
  security_name: string;
  security_type: string;
  asset_class: string;
  amfi_code: string;
  folio_number: string;
  quantity: number;
  avg_cost: number | null;
  cost_basis: number | null;
  invested_amount: number;
  nav: number | null;
  current_value: number | null;
  first_invested_date: string | null;
  last_updated: string | null;
  entity_name?: string;
}

interface HoldingsData {
  entity_id: number;
  entity_name: string;
  total_holdings: number;
  total_invested: number;
  holdings: Holding[];
}

interface Transaction {
  id: number;
  date: string;
  description: string;
  type: string;
  amount: number | null;
  units: number | null;
  nav: number | null;
  balance_units: number | null;
  folio_number: string;
  security_name: string;
  isin: string;
  entity_name?: string;
}

interface TransactionsData {
  entity_id: number;
  total: number;
  limit: number;
  offset: number;
  transactions: Transaction[];
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

const TXN_TYPE_LABELS: Record<string, string> = {
  PURCHASE:   'Purchase',
  REDEMPTION: 'Redemption',
  SWITCH_IN:  'Switch In',
  SWITCH_OUT: 'Switch Out',
  DIVIDEND:   'Dividend',
  SIP:        'SIP',
};

function formatINR(n: number): string {
  const abs = Math.round(Math.abs(n));
  const str = abs.toString();
  const lastThree = str.slice(-3);
  const rest = str.slice(0, -3);
  const grouped =
    rest.length > 0
      ? rest.replace(/\B(?=(\d{2})+(?!\d))/g, ',') + ',' + lastThree
      : lastThree;
  return (n < 0 ? '−₹' : '₹') + grouped;
}

function formatRole(role: string): string {
  return role.charAt(0).toUpperCase() + role.slice(1).toLowerCase();
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString('en-IN', {
    day: 'numeric', month: 'short', year: 'numeric',
  });
}

// ── Skeletons & empty states ───────────────────────────────────────────────

function HoldingsSkeleton() {
  return (
    <section aria-label="MF Holdings" aria-busy="true">
      <span role="status" className="sr-only">Loading holdings</span>
      <div className="bg-card rounded-lg border border-rule overflow-hidden" aria-hidden="true">
        <div className="px-5 sm:px-6 pt-5 pb-4 border-b border-rule">
          <div className="h-3.5 bg-rule rounded w-24 mb-4 animate-pulse" />
          <div className="flex flex-wrap gap-8">
            {[24, 14, 20].map(w => (
              <div key={w} className="space-y-1.5">
                <div className={`h-2.5 bg-rule rounded w-${w} animate-pulse`} />
                <div className="h-4 bg-rule rounded w-28 animate-pulse" />
              </div>
            ))}
          </div>
        </div>
        <div>
          {[...Array(8)].map((_, i) => (
            <div key={i} className="flex gap-4 px-5 sm:px-6 py-3.5 border-t border-rule">
              <div className="flex-1 space-y-1.5">
                <div className="h-3 bg-rule rounded max-w-xs animate-pulse" />
                <div className="h-2 bg-rule rounded w-20 animate-pulse" />
              </div>
              <div className="h-3 bg-rule rounded w-20 animate-pulse self-center" />
              <div className="h-3 bg-rule rounded w-16 animate-pulse self-center" />
              <div className="h-3 bg-rule rounded w-20 animate-pulse self-center" />
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function HoldingsError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <section aria-label="Holdings">
      <div
        role="alert"
        className="bg-card rounded-lg border border-rule px-5 sm:px-6 py-5 flex items-start justify-between gap-4"
      >
        <div>
          <p className="text-sm font-medium text-dim">Could not load holdings</p>
          <p className="text-xs text-ghost mt-1">{message}</p>
        </div>
        <button
          onClick={onRetry}
          className="shrink-0 text-xs border border-wire text-dim px-3 py-1.5 rounded hover:border-dim hover:text-ink transition-colors"
        >
          Retry
        </button>
      </div>
    </section>
  );
}

function HoldingsEmpty() {
  return (
    <section aria-label="MF Holdings">
      <div className="bg-card rounded-lg border border-rule px-6 py-12 text-center">
        <p className="text-sm font-medium text-ink mb-1">No holdings on record</p>
        <p className="text-xs text-ghost mx-auto" style={{ maxWidth: '40ch' }}>
          Holdings will appear once your CAS statement has been imported.
        </p>
      </div>
    </section>
  );
}

// ── Donut chart ────────────────────────────────────────────────────────────

const ASSET_CLASS_COLORS: Record<string, string> = {
  EQUITY:       'var(--chart-equity)',
  FIXED_INCOME: 'var(--chart-fixed)',
  ALTERNATES:   'var(--chart-alt)',
};

interface DonutSegment { cls: string; label: string; value: number; pct: number; }

function DonutChart({ segments }: { segments: DonutSegment[] }) {
  const r = 36, sw = 13, circ = 2 * Math.PI * r, size = 100, c = size / 2;
  const valid = segments.filter(s => s.pct > 0.005);
  let cumLen = 0;
  const arcs = valid.map(seg => {
    const len = seg.pct * circ;
    const dashoffset = circ - cumLen;
    cumLen += len;
    return { ...seg, len, dashoffset };
  });
  return (
    <div className="flex items-center gap-4 shrink-0">
      <svg viewBox={`0 0 ${size} ${size}`} width="72" height="72" aria-hidden="true"
        style={{ transform: 'rotate(-90deg)' }}>
        <circle cx={c} cy={c} r={r} fill="none" stroke="var(--rule)" strokeWidth={sw} />
        {arcs.map(arc => (
          <circle key={arc.cls} cx={c} cy={c} r={r} fill="none" strokeWidth={sw}
            strokeLinecap="butt"
            strokeDasharray={`${arc.len} ${circ - arc.len}`}
            strokeDashoffset={arc.dashoffset}
            style={{ stroke: ASSET_CLASS_COLORS[arc.cls] ?? 'var(--muted)' }} />
        ))}
      </svg>
      <div className="space-y-1.5">
        {valid.map(seg => (
          <div key={seg.cls} className="flex items-center gap-2">
            <span className="w-2 h-2 rounded-full shrink-0"
              style={{ background: ASSET_CLASS_COLORS[seg.cls] ?? 'var(--muted)' }} />
            <span className="text-xs text-ghost whitespace-nowrap">{seg.label}</span>
            <span className="text-xs font-medium text-dim tabular-nums ml-1">
              {Math.round(seg.pct * 100)}%
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Entity switcher (admin only) ───────────────────────────────────────────

function EntitySwitcher({
  entities,
  selectedId,
  onSelect,
}: {
  entities: Entity[];
  selectedId: number | null;
  onSelect: (id: number | null) => void;
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

// ── Holdings section ───────────────────────────────────────────────────────

type SortKey = 'scheme' | 'entity' | 'units' | 'invested' | 'nav' | 'currentValue';
type SortDir = 'asc' | 'desc';

function sortHoldings(holdings: Holding[], key: SortKey, dir: SortDir): Holding[] {
  return [...holdings].sort((a, b) => {
    let va: number | string, vb: number | string;
    switch (key) {
      case 'scheme':
        va = a.security_name.toLowerCase(); vb = b.security_name.toLowerCase(); break;
      case 'entity':
        va = (a.entity_name ?? '').toLowerCase(); vb = (b.entity_name ?? '').toLowerCase(); break;
      case 'units':
        va = a.quantity; vb = b.quantity; break;
      case 'invested':
        va = a.invested_amount; vb = b.invested_amount; break;
      case 'nav':
        va = a.nav ?? -Infinity; vb = b.nav ?? -Infinity; break;
      case 'currentValue':
      default:
        va = a.current_value ?? (a.nav != null ? a.quantity * a.nav : -Infinity);
        vb = b.current_value ?? (b.nav != null ? b.quantity * b.nav : -Infinity);
    }
    if (va < vb) return dir === 'asc' ? -1 : 1;
    if (va > vb) return dir === 'asc' ? 1 : -1;
    return 0;
  });
}

function HoldingsSection({ data, showEntityCol, onRetry }: {
  data: HoldingsData;
  showEntityCol: boolean;
  onRetry: () => void;
}) {
  const [sortKey, setSortKey] = useState<SortKey>('currentValue');
  const [sortDir, setSortDir] = useState<SortDir>('desc');
  const [search, setSearch] = useState('');

  if (data.holdings.length === 0) return <HoldingsEmpty />;

  const colSpan = showEntityCol ? 7 : 6;

  function handleSort(key: SortKey) {
    if (key === sortKey) setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    else { setSortKey(key); setSortDir('desc'); }
  }

  function SortHint({ col }: { col: SortKey }) {
    const active = sortKey === col;
    return (
      <span aria-hidden className={`ml-1 text-[9px] ${active ? 'text-prime' : 'opacity-30'}`}>
        {active ? (sortDir === 'asc' ? '↑' : '↓') : '↕'}
      </span>
    );
  }

  const q = search.toLowerCase();
  const filtered = q
    ? data.holdings.filter(h =>
        h.security_name.toLowerCase().includes(q) ||
        h.folio_number.toLowerCase().includes(q) ||
        (h.entity_name ?? '').toLowerCase().includes(q)
      )
    : data.holdings;

  const grouped: Record<string, Holding[]> = Object.fromEntries(
    ASSET_CLASS_ORDER.map(cls => [cls, filtered.filter(h => h.asset_class === cls)])
  );
  const activeClasses = ASSET_CLASS_ORDER.filter(cls => (grouped[cls]?.length ?? 0) > 0);

  const totalCurrentValue = data.holdings.reduce((sum, h) => {
    const cv = h.current_value ?? (h.nav != null ? Math.round(h.quantity * h.nav) : null);
    return cv != null ? sum + cv : sum;
  }, 0);
  const allHaveNav  = data.holdings.every(h => h.current_value != null || h.nav != null);
  const gain        = allHaveNav && totalCurrentValue > 0 ? totalCurrentValue - data.total_invested : null;
  const gainPct     = gain != null && data.total_invested > 0 ? (gain / data.total_invested) * 100 : null;

  const allocationSegments: DonutSegment[] = ASSET_CLASS_ORDER
    .filter(cls => (grouped[cls]?.length ?? 0) > 0 || data.holdings.some(h => h.asset_class === cls))
    .map(cls => {
      const subtotal = data.holdings.filter(h => h.asset_class === cls)
        .reduce((s, h) => s + h.invested_amount, 0);
      return { cls, label: ASSET_CLASS_LABELS[cls] ?? cls, value: subtotal,
               pct: data.total_invested > 0 ? subtotal / data.total_invested : 0 };
    }).filter(s => s.pct > 0);

  const lastUpdated = data.holdings.map(h => h.last_updated).filter((d): d is string => d != null)
    .sort().at(-1) ?? null;

  return (
    <section aria-label="MF Holdings">
      <div className="bg-card rounded-lg border border-rule overflow-hidden">

        {/* Summary header */}
        <div className="px-5 sm:px-6 pt-5 pb-5 border-b border-rule">
          <div className="flex items-start justify-between gap-3 mb-4">
            <h2 className="text-base font-semibold text-ink">
              MF Holdings
              {lastUpdated && (
                <span className="ml-3 text-xs font-normal text-ghost">
                  as of {formatDate(lastUpdated)}
                </span>
              )}
            </h2>
            <button onClick={onRetry} className="text-xs text-ghost hover:text-dim transition-colors">
              Refresh
            </button>
          </div>
          <div className="flex flex-col sm:flex-row sm:items-start gap-5">
            <div className="flex flex-wrap gap-x-8 gap-y-3 flex-1">
              <div>
                <p className="text-xs text-ghost mb-0.5">Total Invested</p>
                <p className="text-sm font-semibold text-ink tabular-nums">{formatINR(data.total_invested)}</p>
              </div>
              {totalCurrentValue > 0 && (
                <div>
                  <p className="text-xs text-ghost mb-0.5">Current Value</p>
                  <p className="text-sm font-semibold text-ink tabular-nums">{formatINR(totalCurrentValue)}</p>
                </div>
              )}
              {gain != null && gainPct != null && (
                <div>
                  <p className="text-xs text-ghost mb-0.5">Unrealized P&amp;L</p>
                  <p className="text-sm font-semibold tabular-nums"
                    style={{ color: gain >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
                    {gain >= 0 ? '+' : '−'}{formatINR(Math.abs(gain))}{' '}
                    <span className="text-xs font-medium opacity-75">
                      ({gain >= 0 ? '+' : '−'}{Math.abs(gainPct).toFixed(1)}%)
                    </span>
                  </p>
                </div>
              )}
              <div>
                <p className="text-xs text-ghost mb-0.5">Holdings</p>
                <p className="text-sm font-semibold text-ink tabular-nums">{data.total_holdings}</p>
              </div>
            </div>
            {allocationSegments.length > 1 && <DonutChart segments={allocationSegments} />}
          </div>
        </div>

        {/* Search bar */}
        <div className="px-5 sm:px-6 py-3 border-b border-rule">
          <input
            type="search"
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="Search scheme, folio or entity…"
            aria-label="Search holdings"
            className="w-full max-w-sm text-xs bg-page border border-wire rounded px-3 py-1.5 text-ink placeholder:text-ghost focus:outline-none focus:border-prime transition-colors"
          />
        </div>

        {/* Table */}
        <div className="overflow-x-auto" role="region" aria-label="Holdings table" tabIndex={0}>
          <table className="w-full text-sm" style={{ minWidth: showEntityCol ? '820px' : '700px' }}>
            <thead>
              <tr className="sticky top-0 z-10">
                <th scope="col"
                  className="text-left px-5 sm:px-6 py-3 text-xs font-medium text-ghost bg-card border-b border-rule"
                  aria-sort={sortKey === 'scheme' ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}>
                  <button onClick={() => handleSort('scheme')}
                    className="inline-flex items-center hover:text-ink transition-colors">
                    Scheme<SortHint col="scheme" />
                  </button>
                </th>
                {showEntityCol && (
                  <th scope="col"
                    className="text-left px-4 py-3 text-xs font-medium text-ghost bg-card border-b border-rule whitespace-nowrap"
                    aria-sort={sortKey === 'entity' ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}>
                    <button onClick={() => handleSort('entity')}
                      className="inline-flex items-center hover:text-ink transition-colors">
                      Entity<SortHint col="entity" />
                    </button>
                  </th>
                )}
                <th scope="col"
                  className="text-left px-4 py-3 text-xs font-medium text-ghost bg-card border-b border-rule whitespace-nowrap">
                  Folio
                </th>
                <th scope="col"
                  className="text-right px-4 py-3 text-xs font-medium text-ghost bg-card border-b border-rule whitespace-nowrap"
                  aria-sort={sortKey === 'units' ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}>
                  <button onClick={() => handleSort('units')}
                    className="inline-flex items-center ml-auto hover:text-ink transition-colors">
                    Units<SortHint col="units" />
                  </button>
                </th>
                <th scope="col"
                  className="text-right px-4 py-3 text-xs font-medium text-ghost bg-card border-b border-rule whitespace-nowrap"
                  aria-sort={sortKey === 'invested' ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}>
                  <button onClick={() => handleSort('invested')}
                    className="inline-flex items-center ml-auto hover:text-ink transition-colors">
                    Invested<SortHint col="invested" />
                  </button>
                </th>
                <th scope="col"
                  className="text-right px-4 py-3 text-xs font-medium text-ghost bg-card border-b border-rule whitespace-nowrap"
                  aria-sort={sortKey === 'nav' ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}>
                  <button onClick={() => handleSort('nav')}
                    className="inline-flex items-center ml-auto hover:text-ink transition-colors">
                    NAV<SortHint col="nav" />
                  </button>
                </th>
                <th scope="col"
                  className="text-right px-5 sm:px-6 py-3 text-xs font-medium text-ghost bg-card border-b border-rule whitespace-nowrap"
                  aria-sort={sortKey === 'currentValue' ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}>
                  <button onClick={() => handleSort('currentValue')}
                    className="inline-flex items-center ml-auto hover:text-ink transition-colors">
                    Current Value<SortHint col="currentValue" />
                  </button>
                </th>
              </tr>
            </thead>
            <tbody>
              {activeClasses.length === 0 ? (
                <tr>
                  <td colSpan={colSpan} className="px-5 sm:px-6 py-8 text-center text-xs text-ghost">
                    No holdings match your search.
                  </td>
                </tr>
              ) : activeClasses.map(cls => {
                const group  = grouped[cls];
                if (!group || group.length === 0) return null;
                const subtotal = group.reduce((s, h) => s + h.invested_amount, 0);
                const sorted   = sortHoldings(group, sortKey, sortDir);
                return (
                  <Fragment key={cls}>
                    <tr>
                      <td colSpan={colSpan} className="px-5 sm:px-6 py-2 bg-page">
                        <span className="text-xs font-semibold text-dim">
                          {ASSET_CLASS_LABELS[cls] ?? cls}
                        </span>
                        <span className="ml-2 text-xs text-ghost">{formatINR(subtotal)}</span>
                      </td>
                    </tr>
                    {sorted.map(h => {
                      const currentVal =
                        h.current_value ??
                        (h.nav != null ? Math.round(h.quantity * h.nav) : null);
                      return (
                        <tr key={h.id}
                          className="border-t border-rule hover:bg-page transition-colors duration-100">
                          <td className="px-5 sm:px-6 py-3.5">
                            <p className="text-ink leading-snug">{h.security_name}</p>
                            <p className="text-xs text-ghost mt-0.5">
                              {SEC_TYPE_LABELS[h.security_type] ?? h.security_type}
                            </p>
                          </td>
                          {showEntityCol && (
                            <td className="px-4 py-3.5 text-xs font-medium text-dim whitespace-nowrap align-top">
                              {h.entity_name ?? '—'}
                            </td>
                          )}
                          <td className="px-4 py-3.5 font-mono text-xs text-dim whitespace-nowrap align-top">
                            {h.folio_number}
                          </td>
                          <td className="px-4 py-3.5 text-right tabular-nums text-ink whitespace-nowrap align-top">
                            {h.quantity.toFixed(3)}
                          </td>
                          <td className="px-4 py-3.5 text-right tabular-nums text-ink whitespace-nowrap align-top">
                            {formatINR(h.invested_amount)}
                          </td>
                          <td className="px-4 py-3.5 text-right tabular-nums text-dim whitespace-nowrap align-top">
                            {h.nav != null ? '₹' + h.nav.toFixed(2) : '—'}
                          </td>
                          <td className="px-5 sm:px-6 py-3.5 text-right tabular-nums text-ink whitespace-nowrap align-top">
                            {currentVal != null ? formatINR(currentVal) : '—'}
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
    </section>
  );
}

// ── Analytics section ─────────────────────────────────────────────────────

function computeCAGR(invested: number, current: number, firstDate: string | null): number | null {
  if (!firstDate || invested <= 0 || current <= 0) return null;
  const days = (Date.now() - new Date(firstDate).getTime()) / 86_400_000;
  if (days < 60) return null;
  return (Math.pow(current / invested, 365.25 / days) - 1) * 100;
}

const ARBITRAGE_TYPES = new Set(['MF_ARBITRAGE']);

interface FundRow {
  id: number;
  security_name: string;
  security_type: string;
  asset_class: string;
  folio_number: string;
  entity_name?: string;
  invested_amount: number;
  current_value: number;
  gain: number;
  return_pct: number;
  weight_pct: number;
  cagr_pct: number | null;
  first_invested_date: string | null;
}

function FundBar({ fund, maxPct }: { fund: FundRow; maxPct: number }) {
  const isArb = ARBITRAGE_TYPES.has(fund.security_type);
  const color = isArb
    ? 'var(--chart-alt)'
    : ASSET_CLASS_COLORS[fund.asset_class] ?? 'var(--muted)';
  const barWidth = maxPct > 0 ? (fund.weight_pct / maxPct) * 100 : 0;
  return (
    <div className="flex items-center gap-3 py-1">
      <div className="w-48 sm:w-64 shrink-0">
        <p className="text-xs text-ink truncate leading-none" title={fund.security_name}>
          {fund.security_name}
        </p>
        {fund.entity_name && (
          <p className="text-[10px] text-ghost mt-0.5">{fund.entity_name}</p>
        )}
      </div>
      <div className="flex-1 h-2 bg-rule rounded-full overflow-hidden">
        <div className="h-full rounded-full" style={{ width: `${barWidth}%`, background: color }} />
      </div>
      <span className="w-11 text-right text-xs tabular-nums text-dim shrink-0">
        {fund.weight_pct.toFixed(1)}%
      </span>
      <span className="w-24 text-right text-xs tabular-nums text-ink shrink-0">
        {formatINR(fund.current_value)}
      </span>
    </div>
  );
}

function AnalyticsFundTable({ funds, showEntityCol }: {
  funds: FundRow[];
  showEntityCol: boolean;
}) {
  const colSpan = showEntityCol ? 9 : 8;
  return (
    <div className="overflow-x-auto" role="region" aria-label="Fund analytics table" tabIndex={0}>
      <table className="w-full text-sm" style={{ minWidth: showEntityCol ? '900px' : '780px' }}>
        <thead>
          <tr className="sticky top-0 z-10">
            {['Fund', ...(showEntityCol ? ['Entity'] : []),
              'Type', 'Invested', 'Current Value', 'Gain / Loss', 'Return %', 'Weight %', 'CAGR'
            ].map((col, i) => (
              <th key={col} scope="col"
                className={`py-3 text-xs font-medium text-ghost bg-card border-b border-rule whitespace-nowrap
                  ${i === 0 ? 'text-left px-5 sm:px-6' : i <= (showEntityCol ? 2 : 1) ? 'text-left px-4' : 'text-right px-4'}
                  ${col === 'CAGR' ? 'pr-5 sm:pr-6' : ''}`}>
                {col}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {funds.map(f => {
            const positive = f.gain >= 0;
            return (
              <tr key={f.id}
                className="border-t border-rule hover:bg-page transition-colors duration-100">
                <td className="px-5 sm:px-6 py-3 align-top">
                  <p className="text-xs text-ink leading-snug">{f.security_name}</p>
                  <p className="text-[10px] text-ghost mt-0.5 font-mono">{f.folio_number}</p>
                </td>
                {showEntityCol && (
                  <td className="px-4 py-3 text-xs font-medium text-dim whitespace-nowrap align-top">
                    {f.entity_name ?? '—'}
                  </td>
                )}
                <td className="px-4 py-3 align-top">
                  <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-rule text-dim whitespace-nowrap">
                    {SEC_TYPE_LABELS[f.security_type] ?? f.security_type}
                  </span>
                </td>
                <td className="px-4 py-3 text-right tabular-nums text-xs text-ink whitespace-nowrap align-top">
                  {formatINR(f.invested_amount)}
                </td>
                <td className="px-4 py-3 text-right tabular-nums text-xs text-ink whitespace-nowrap align-top">
                  {formatINR(f.current_value)}
                </td>
                <td className="px-4 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top"
                  style={{ color: positive ? 'var(--gain)' : 'var(--peril)' }}>
                  {positive ? '+' : '−'}{formatINR(Math.abs(f.gain))}
                </td>
                <td className="px-4 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top"
                  style={{ color: positive ? 'var(--gain)' : 'var(--peril)' }}>
                  {positive ? '+' : ''}{f.return_pct.toFixed(2)}%
                </td>
                <td className="px-4 py-3 text-right tabular-nums text-xs text-dim whitespace-nowrap align-top">
                  {f.weight_pct.toFixed(2)}%
                </td>
                <td className="px-4 pr-5 sm:pr-6 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top"
                  style={{ color: f.cagr_pct != null ? (f.cagr_pct >= 0 ? 'var(--gain)' : 'var(--peril)') : undefined }}>
                  {f.cagr_pct != null
                    ? `${f.cagr_pct >= 0 ? '+' : ''}${f.cagr_pct.toFixed(2)}% p.a.`
                    : '—'}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function AnalyticsSection({ data, showEntityCol }: {
  data: HoldingsData;
  showEntityCol: boolean;
}) {
  if (data.holdings.length === 0) return null;

  const funds: FundRow[] = data.holdings.map(h => {
    const cv = h.current_value ?? (h.nav != null ? Math.round(h.quantity * h.nav) : h.invested_amount);
    const gain = cv - h.invested_amount;
    return {
      id:                 h.id,
      security_name:      h.security_name,
      security_type:      h.security_type,
      asset_class:        h.asset_class,
      folio_number:       h.folio_number,
      entity_name:        h.entity_name,
      invested_amount:    h.invested_amount,
      current_value:      cv,
      gain,
      return_pct:         h.invested_amount > 0 ? (gain / h.invested_amount) * 100 : 0,
      weight_pct:         0,
      cagr_pct:           computeCAGR(h.invested_amount, cv, h.first_invested_date),
      first_invested_date: h.first_invested_date,
    };
  });

  const totalValue = funds.reduce((s, f) => s + f.current_value, 0);
  funds.forEach(f => { f.weight_pct = totalValue > 0 ? (f.current_value / totalValue) * 100 : 0; });

  const sorted      = [...funds].sort((a, b) => b.weight_pct - a.weight_pct);
  const maxPct      = sorted[0]?.weight_pct ?? 1;
  const arbFunds    = sorted.filter(f => ARBITRAGE_TYPES.has(f.security_type));
  const regularFunds = sorted.filter(f => !ARBITRAGE_TYPES.has(f.security_type));

  const assetSegments: DonutSegment[] = ASSET_CLASS_ORDER.map(cls => {
    const v = funds.filter(f => f.asset_class === cls).reduce((s, f) => s + f.current_value, 0);
    return { cls, label: ASSET_CLASS_LABELS[cls] ?? cls, value: v,
             pct: totalValue > 0 ? v / totalValue : 0 };
  }).filter(s => s.pct > 0.001);

  const totalGain   = funds.reduce((s, f) => s + f.gain, 0);
  const totalReturn = data.total_invested > 0 ? (totalGain / data.total_invested) * 100 : 0;

  return (
    <section aria-label="Portfolio Analytics">
      <div className="bg-card rounded-lg border border-rule overflow-hidden">

        {/* Header */}
        <div className="px-5 sm:px-6 pt-5 pb-4 border-b border-rule">
          <h2 className="text-base font-semibold text-ink mb-1">Portfolio Analytics</h2>
          <p className="text-xs text-ghost">{data.entity_name}</p>
        </div>

        {/* Summary strip */}
        <div className="px-5 sm:px-6 py-4 border-b border-rule flex flex-wrap gap-x-8 gap-y-3">
          <div>
            <p className="text-xs text-ghost mb-0.5">Total Invested</p>
            <p className="text-sm font-semibold text-ink tabular-nums">{formatINR(data.total_invested)}</p>
          </div>
          <div>
            <p className="text-xs text-ghost mb-0.5">Current Value</p>
            <p className="text-sm font-semibold text-ink tabular-nums">{formatINR(totalValue)}</p>
          </div>
          <div>
            <p className="text-xs text-ghost mb-0.5">Total Gain / Loss</p>
            <p className="text-sm font-semibold tabular-nums"
              style={{ color: totalGain >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
              {totalGain >= 0 ? '+' : '−'}{formatINR(Math.abs(totalGain))}
            </p>
          </div>
          <div>
            <p className="text-xs text-ghost mb-0.5">Overall Return</p>
            <p className="text-sm font-semibold tabular-nums"
              style={{ color: totalReturn >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
              {totalReturn >= 0 ? '+' : ''}{totalReturn.toFixed(2)}%
            </p>
          </div>
          <div>
            <p className="text-xs text-ghost mb-0.5">Funds</p>
            <p className="text-sm font-semibold text-ink tabular-nums">{funds.length}</p>
          </div>
        </div>

        {/* Donut + bar chart side by side */}
        <div className="p-5 sm:p-6 border-b border-rule grid grid-cols-1 lg:grid-cols-5 gap-8">
          {/* Donut */}
          <div className="lg:col-span-2">
            <p className="text-xs font-medium text-ghost uppercase tracking-wide mb-4">Asset Allocation</p>
            <div className="flex items-center gap-6">
              <DonutChart segments={assetSegments} />
              <div className="space-y-3 flex-1">
                {assetSegments.map(seg => (
                  <div key={seg.cls}>
                    <div className="flex justify-between text-xs mb-1">
                      <span className="text-ghost">{seg.label}</span>
                      <span className="tabular-nums font-medium text-dim">
                        {(seg.pct * 100).toFixed(1)}%
                      </span>
                    </div>
                    <div className="h-1 bg-rule rounded-full overflow-hidden">
                      <div className="h-full rounded-full"
                        style={{ width: `${seg.pct * 100}%`,
                                 background: ASSET_CLASS_COLORS[seg.cls] ?? 'var(--muted)' }} />
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>

          {/* Fund weight bars */}
          <div className="lg:col-span-3">
            <p className="text-xs font-medium text-ghost uppercase tracking-wide mb-4">
              Fund Breakdown — % of Portfolio
            </p>
            <div className="space-y-1.5 max-h-72 overflow-y-auto pr-1">
              {sorted.map(f => (
                <FundBar key={f.id} fund={f} maxPct={maxPct} />
              ))}
            </div>
            <div className="flex items-center gap-4 mt-4 flex-wrap">
              {[
                { label: 'Equity',       cls: 'EQUITY' },
                { label: 'Fixed Income', cls: 'FIXED_INCOME' },
                { label: 'Alternates',   cls: 'ALTERNATES' },
                { label: 'Arbitrage',    color: 'var(--chart-alt)', note: true },
              ].map(item => (
                <div key={item.label} className="flex items-center gap-1.5">
                  <span className="w-2 h-2 rounded-full shrink-0"
                    style={{ background: 'note' in item
                      ? item.color
                      : ASSET_CLASS_COLORS[item.cls!] ?? 'var(--muted)' }} />
                  <span className="text-[10px] text-ghost">{item.label}</span>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* Arbitrage funds section */}
        {arbFunds.length > 0 && (
          <div className="border-b border-rule">
            <div className="px-5 sm:px-6 py-3 bg-page flex items-center gap-2">
              <span className="w-2 h-2 rounded-full shrink-0"
                style={{ background: 'var(--chart-alt)' }} />
              <span className="text-xs font-semibold text-dim">
                Arbitrage Funds
              </span>
              <span className="text-xs text-ghost ml-1">
                — equity savings / debt arbitrage category · {arbFunds.length} fund{arbFunds.length > 1 ? 's' : ''}
              </span>
            </div>
            <div className="px-5 sm:px-6 py-4 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
              {arbFunds.map(f => (
                <div key={f.id} className="rounded-lg border border-rule p-4">
                  <p className="text-xs font-medium text-ink leading-snug mb-3">{f.security_name}</p>
                  <div className="grid grid-cols-2 gap-x-4 gap-y-2">
                    {[
                      { label: 'Invested',     value: formatINR(f.invested_amount),   color: undefined },
                      { label: 'Current',      value: formatINR(f.current_value),      color: undefined },
                      { label: 'Gain / Loss',  value: `${f.gain >= 0 ? '+' : '−'}${formatINR(Math.abs(f.gain))}`,
                        color: f.gain >= 0 ? 'var(--gain)' : 'var(--peril)' },
                      { label: 'Return',       value: `${f.return_pct >= 0 ? '+' : ''}${f.return_pct.toFixed(2)}%`,
                        color: f.return_pct >= 0 ? 'var(--gain)' : 'var(--peril)' },
                      { label: 'Weight',       value: `${f.weight_pct.toFixed(2)}%`,  color: undefined },
                      { label: 'CAGR p.a.',   value: f.cagr_pct != null
                          ? `${f.cagr_pct >= 0 ? '+' : ''}${f.cagr_pct.toFixed(2)}%`
                          : '—',
                        color: f.cagr_pct != null
                          ? f.cagr_pct >= 0 ? 'var(--gain)' : 'var(--peril)'
                          : undefined },
                    ].map(({ label, value, color }) => (
                      <div key={label}>
                        <p className="text-[10px] text-ghost mb-0.5">{label}</p>
                        <p className="text-xs font-medium tabular-nums"
                          style={{ color: color ?? 'var(--ink)' }}>
                          {value}
                        </p>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Full fund performance table */}
        <div>
          <div className="px-5 sm:px-6 py-3 bg-page border-b border-rule">
            <span className="text-xs font-semibold text-dim">All Funds — Performance Detail</span>
          </div>
          <AnalyticsFundTable funds={sorted} showEntityCol={showEntityCol} />
        </div>
      </div>
    </section>
  );
}

// ── Transactions section ───────────────────────────────────────────────────

function TransactionsSection({ data, showEntityCol }: {
  data: TransactionsData;
  showEntityCol: boolean;
}) {
  if (data.transactions.length === 0) {
    return (
      <section aria-label="Recent Transactions">
        <div className="bg-card rounded-lg border border-rule px-6 py-8 text-center">
          <p className="text-sm font-medium text-ink mb-1">No transactions on record</p>
          <p className="text-xs text-ghost">Transactions will appear after the first CAS import.</p>
        </div>
      </section>
    );
  }

  const colSpan = showEntityCol ? 8 : 7;

  return (
    <section aria-label="Recent Transactions">
      <div className="bg-card rounded-lg border border-rule overflow-hidden">
        <div className="px-5 sm:px-6 pt-5 pb-4 border-b border-rule">
          <h2 className="text-base font-semibold text-ink">
            Recent Transactions
            <span className="ml-3 text-xs font-normal text-ghost">
              {data.total.toLocaleString('en-IN')} total
            </span>
          </h2>
        </div>
        <div className="overflow-x-auto" role="region" aria-label="Transactions table" tabIndex={0}>
          <table className="w-full text-sm" style={{ minWidth: showEntityCol ? '920px' : '800px' }}>
            <thead>
              <tr className="sticky top-0 z-10">
                {['Date', ...(showEntityCol ? ['Entity'] : []),
                  'Scheme', 'Folio', 'Type', 'Units', 'NAV', 'Amount'].map(col => (
                  <th key={col} scope="col"
                    className={`py-3 text-xs font-medium text-ghost bg-card border-b border-rule whitespace-nowrap
                      ${col === 'Date' || col === 'Scheme' || col === 'Entity' || col === 'Folio' || col === 'Type'
                        ? 'text-left px-4 first:pl-5 sm:first:pl-6'
                        : 'text-right px-4 last:pr-5 sm:last:pr-6'}`}>
                    {col}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.transactions.map(t => (
                <tr key={t.id} className="border-t border-rule hover:bg-page transition-colors duration-100">
                  <td className="px-4 pl-5 sm:pl-6 py-3 text-xs text-dim whitespace-nowrap align-top">
                    {formatDate(t.date)}
                  </td>
                  {showEntityCol && (
                    <td className="px-4 py-3 text-xs font-medium text-dim whitespace-nowrap align-top">
                      {t.entity_name ?? '—'}
                    </td>
                  )}
                  <td className="px-4 py-3 align-top">
                    <p className="text-ink leading-snug text-xs">{t.security_name}</p>
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-ghost whitespace-nowrap align-top">
                    {t.folio_number}
                  </td>
                  <td className="px-4 py-3 align-top">
                    <span className={`inline-block text-[10px] font-medium px-1.5 py-0.5 rounded whitespace-nowrap
                      ${t.type === 'PURCHASE' || t.type === 'SIP' || t.type === 'SWITCH_IN'
                        ? 'bg-gain/10 text-gain'
                        : t.type === 'REDEMPTION' || t.type === 'SWITCH_OUT'
                        ? 'bg-peril/10 text-peril'
                        : 'bg-rule text-dim'}`}>
                      {TXN_TYPE_LABELS[t.type] ?? t.type}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right tabular-nums text-xs text-ink whitespace-nowrap align-top">
                    {t.units != null ? t.units.toFixed(3) : '—'}
                  </td>
                  <td className="px-4 py-3 text-right tabular-nums text-xs text-dim whitespace-nowrap align-top">
                    {t.nav != null ? '₹' + t.nav.toFixed(2) : '—'}
                  </td>
                  <td className="px-4 pr-5 sm:pr-6 py-3 text-right tabular-nums text-xs text-ink whitespace-nowrap align-top">
                    {t.amount != null ? formatINR(t.amount) : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {data.total > data.transactions.length && (
          <div className="px-5 sm:px-6 py-3 border-t border-rule text-center">
            <p className="text-xs text-ghost">
              Showing {data.transactions.length} of {data.total.toLocaleString('en-IN')} transactions
            </p>
          </div>
        )}
      </div>
    </section>
  );
}

// ── Dashboard page ─────────────────────────────────────────────────────────

export default function DashboardPage() {
  const [user, setUser]               = useState<User | null>(null);
  const [error, setError]             = useState<string | null>(null);
  const [loggingOut, setLoggingOut]   = useState(false);

  const [entities, setEntities]               = useState<Entity[]>([]);
  const [selectedEntityId, setSelectedEntityId] = useState<number | null>(null);

  const [holdings, setHoldings]               = useState<HoldingsData | null>(null);
  const [holdingsLoading, setHoldingsLoading] = useState(true);
  const [holdingsError, setHoldingsError]     = useState<string | null>(null);
  const [retryCount, setRetryCount]           = useState(0);

  const [transactions, setTransactions]               = useState<TransactionsData | null>(null);
  const [transactionsLoading, setTransactionsLoading] = useState(true);
  const [transactionsError, setTransactionsError]     = useState<string | null>(null);

  const router = useRouter();

  const handleLogout = useCallback(async () => {
    setLoggingOut(true);
    try {
      const res = await fetch(`${API_URL}/api/v1/auth/logout`, {
        method: 'POST', credentials: 'include',
      });
      if (!res.ok) { setError('Sign out failed. Please try again.'); setLoggingOut(false); return; }
    } catch {
      setError('Network error during sign out. Please try again.'); setLoggingOut(false); return;
    }
    router.push('/');
  }, [router]);

  // Idle timeout
  useEffect(() => {
    let idleTimer: NodeJS.Timeout;
    const resetTimer = () => {
      clearTimeout(idleTimer);
      idleTimer = setTimeout(handleLogout, IDLE_TIMEOUT);
    };
    const events = ['mousedown', 'keypress', 'touchstart'] as const;
    events.forEach(e => window.addEventListener(e, resetTimer));
    window.addEventListener('scroll', resetTimer, { passive: true });
    resetTimer();
    return () => {
      clearTimeout(idleTimer);
      events.forEach(e => window.removeEventListener(e, resetTimer));
      window.removeEventListener('scroll', resetTimer);
    };
  }, [handleLogout]);

  // Fetch session user; if admin, also fetch entity list
  useEffect(() => {
    const controller = new AbortController();
    fetch(`${API_URL}/api/v1/me`, { credentials: 'include', signal: controller.signal })
      .then(res => {
        if (res.status === 401) { router.push('/'); return null; }
        if (!res.ok) throw new Error('Unable to load your session. Please try again.');
        return res.json();
      })
      .then((data: User | null) => {
        if (!data) return;
        setUser(data);
        if (data.role === 'admin') {
          fetch(`${API_URL}/api/v1/entities`, { credentials: 'include' })
            .then(r => r.ok ? r.json() : [])
            .then((ents: Entity[]) => setEntities(ents))
            .catch(() => {});
        }
      })
      .catch(err => {
        if (err.name === 'AbortError') return;
        setError(err.message);
        setTimeout(() => router.push('/'), 3000);
      });
    return () => controller.abort();
  }, [router]);

  // Fetch holdings whenever entity selection or retry changes
  useEffect(() => {
    const controller = new AbortController();
    setHoldingsLoading(true);
    setHoldingsError(null);
    const qs = selectedEntityId !== null ? `?entity_id=${selectedEntityId}` : '';
    fetch(`${API_URL}/api/v1/holdings${qs}`, { credentials: 'include', signal: controller.signal })
      .then(res => {
        if (res.status === 401) return null;
        if (!res.ok) throw new Error('Unable to load holdings.');
        return res.json();
      })
      .then(data => {
        if (data) setHoldings(data);
        setHoldingsLoading(false);
      })
      .catch(err => {
        if (err.name === 'AbortError') return;
        setHoldingsError(err.message);
        setHoldingsLoading(false);
      });
    return () => controller.abort();
  }, [selectedEntityId, retryCount]);

  // Fetch transactions whenever entity selection or retry changes
  useEffect(() => {
    const controller = new AbortController();
    setTransactionsLoading(true);
    setTransactionsError(null);
    const base = selectedEntityId !== null
      ? `?entity_id=${selectedEntityId}&limit=100`
      : '?limit=100';
    fetch(`${API_URL}/api/v1/transactions${base}`, { credentials: 'include', signal: controller.signal })
      .then(res => {
        if (res.status === 401) return null;
        if (!res.ok) throw new Error('Unable to load transactions.');
        return res.json();
      })
      .then(data => {
        if (data) setTransactions(data);
        setTransactionsLoading(false);
      })
      .catch(err => {
        if (err.name === 'AbortError') return;
        setTransactionsError(err.message);
        setTransactionsLoading(false);
      });
    return () => controller.abort();
  }, [selectedEntityId, retryCount]);

  const isAdmin       = user?.role === 'admin';
  const showEntityCol = isAdmin && selectedEntityId === null;
  const handleRetry   = useCallback(() => setRetryCount(c => c + 1), []);

  if (error) return (
    <main id="main-content" className="min-h-screen flex items-center justify-center bg-page px-4 py-10">
      <div className="bg-card p-6 sm:p-8 rounded-lg shadow-sm border border-rule text-center w-full max-w-sm" role="alert">
        <p className="text-peril font-medium mb-2">Session error</p>
        <p className="text-dim text-sm">{error}</p>
        <p className="text-ghost text-xs mt-2">
          Redirecting to login, or{' '}
          <a href="/" className="text-prime underline-offset-2 hover:underline">go now</a>.
        </p>
      </div>
    </main>
  );

  if (!user) return (
    <main id="main-content" className="min-h-screen bg-page p-4 sm:p-8" aria-busy="true">
      <span role="status" aria-live="polite" className="sr-only">Loading dashboard</span>
      <div className="max-w-6xl mx-auto">
        <div className="flex flex-wrap justify-between items-start gap-3 mb-6" aria-hidden="true">
          <div className="space-y-2">
            <div className="h-8 sm:h-9 bg-rule rounded-md w-52 animate-pulse" />
            <div className="h-3 bg-rule rounded w-44 animate-pulse" />
          </div>
          <div className="h-9 bg-rule rounded-md w-24 animate-pulse" />
        </div>
        <HoldingsSkeleton />
      </div>
    </main>
  );

  return (
    <main id="main-content" className="min-h-screen bg-page p-4 sm:p-8">
      <div className="max-w-6xl mx-auto">

        {/* Header */}
        <div className="flex flex-wrap justify-between items-start gap-3 mb-6">
          <div className="min-w-0 flex-1">
            <h1 className="text-2xl sm:text-3xl font-bold text-ink wrap-break-word">
              Welcome, {user.full_name}
            </h1>
            <p className="text-sm text-ghost mt-0.5">{user.email} · {formatRole(user.role)}</p>
          </div>
          <button
            onClick={handleLogout}
            disabled={loggingOut}
            aria-busy={loggingOut}
            className="border border-wire text-dim px-4 py-2 rounded-md text-sm hover:border-dim hover:text-ink disabled:opacity-50 disabled:cursor-not-allowed transition-colors duration-150 shrink-0"
          >
            {loggingOut ? 'Signing out...' : 'Sign out'}
          </button>
        </div>

        {/* Entity switcher — admin only */}
        {isAdmin && entities.length > 0 && (
          <EntitySwitcher
            entities={entities}
            selectedId={selectedEntityId}
            onSelect={setSelectedEntityId}
          />
        )}

        {/* Holdings */}
        <div className="mb-5">
          {holdingsLoading && !holdingsError && <HoldingsSkeleton />}
          {holdingsError && <HoldingsError message={holdingsError} onRetry={handleRetry} />}
          {!holdingsLoading && !holdingsError && holdings && (
            <HoldingsSection data={holdings} showEntityCol={showEntityCol} onRetry={handleRetry} />
          )}
        </div>

        {/* Analytics */}
        {!holdingsLoading && !holdingsError && holdings && holdings.holdings.length > 0 && (
          <div className="mb-5">
            <AnalyticsSection data={holdings} showEntityCol={showEntityCol} />
          </div>
        )}

        {/* Transactions */}
        {!transactionsLoading && !transactionsError && transactions && (
          <div className="mb-4">
            <TransactionsSection data={transactions} showEntityCol={showEntityCol} />
          </div>
        )}
        {transactionsError && (
          <div role="alert" className="mb-4 bg-card rounded-lg border border-rule px-5 py-4 flex items-center justify-between gap-4">
            <p className="text-xs text-ghost">Could not load transactions — {transactionsError}</p>
            <button onClick={handleRetry}
              className="shrink-0 text-xs border border-wire text-dim px-3 py-1.5 rounded hover:border-dim hover:text-ink transition-colors">
              Retry
            </button>
          </div>
        )}

        <p className="text-center text-xs text-ghost mt-8">
          IWS Finserv &copy; {new Date().getFullYear()} · Session expires after 30 minutes of inactivity
        </p>
      </div>
    </main>
  );
}
