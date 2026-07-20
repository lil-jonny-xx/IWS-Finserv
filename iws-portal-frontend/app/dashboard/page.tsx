'use client';
import { useEffect, useState, useCallback, useMemo } from 'react';
import { useRouter } from 'next/navigation';
import Image from 'next/image';
import NavTabs from '@/app/components/NavTabs';
import EntitySwitcher from '@/app/components/EntitySwitcher';
import MarketRail from '@/app/components/MarketRail';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

// ── types ──────────────────────────────────────────────────────────────────────

interface User { full_name: string; email: string; role: string; }

interface AssetClassItem {
  asset_class: string;
  invested: number;
  value: number;
  pnl: number;
  pct: number;
}

interface EntitySummary {
  entity_id: number;
  entity_name: string;
  total_invested: number;
  total_value: number;
  total_pnl: number;
  total_pnl_ytd: number;
  total_weekly: number;
  asset_classes: AssetClassItem[];
}

interface OverviewData {
  summary: {
    total_invested: number;
    total_value: number;
    total_pnl: number;
    total_pnl_ytd: number;
    total_weekly: number;
    weighted_cagr: number | null;
  };
  asset_class_breakdown: AssetClassItem[];
  entities: EntitySummary[];
}

// ── formatters ────────────────────────────────────────────────────────────────

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

function fmtINRCompact(n: number): string {
  if (Math.abs(n) >= 1e7) return (n < 0 ? '−₹' : '₹') + (Math.abs(n) / 1e7).toFixed(2) + ' Cr';
  if (Math.abs(n) >= 1e5) return (n < 0 ? '−₹' : '₹') + (Math.abs(n) / 1e5).toFixed(2) + ' L';
  return fmtINR(n);
}

function fmtPct(n: number | null | undefined): string {
  if (n == null) return '—';
  return (n >= 0 ? '+' : '') + n.toFixed(2) + '%';
}

function gainColor(n: number): string {
  return n >= 0 ? 'var(--gain)' : 'var(--peril)';
}

// ── asset class config ────────────────────────────────────────────────────────

const CLASS_COLORS: Record<string, string> = {
  EQUITY:        'var(--chart-equity)',
  DIRECT_EQUITY: '#6366f1',   // indigo for direct stocks
  PMS:           '#f59e0b',   // amber for PMS portfolios
  FIXED_INCOME:  'var(--chart-fixed)',
  ALTERNATES:    'var(--chart-alt)',
  REAL_ESTATE:   '#0ea5e9',   // sky for real estate / property
  GOLD_SILVER:   '#d4af37',   // gold for precious metals
  COMMODITIES:   '#b45309',   // burnt amber for commodities
  ART:           '#db2777',   // pink for art / collectibles
  HYBRID:        '#8b5cf6',   // violet for hybrid MF
  ARBITRAGE:     '#14b8a6',   // teal for arbitrage
  CASH:          '#94a3b8',   // slate for cash / transit balances
  BANK_CASH:     '#64748b',   // darker slate for bank-account cash
  MF:            'var(--chart-equity)',
};

const CLASS_LABELS: Record<string, string> = {
  EQUITY:        'Equity MF',
  DIRECT_EQUITY: 'Direct Equity',
  PMS:           'PMS',
  FIXED_INCOME:  'Fixed Income',
  ALTERNATES:    'Alternates',
  REAL_ESTATE:   'Real Estate',
  GOLD_SILVER:   'Gold / Silver',
  COMMODITIES:   'Commodities',
  ART:           'Art',
  HYBRID:        'Hybrid MF',
  ARBITRAGE:     'Arbitrage',
  CASH:          'Cash & Transit',
  BANK_CASH:     'Bank Accounts',
  MF:            'Mutual Funds',
};

// ── donut chart ───────────────────────────────────────────────────────────────

function DonutChart({ segments }: { segments: { key: string; label: string; value: number; pct: number }[] }) {
  const r = 38, sw = 14, circ = 2 * Math.PI * r, size = 100, c = 50;
  const valid = segments.filter(s => s.pct > 0.005);
  let cum = 0;
  const arcs = valid.map(seg => {
    const len = seg.pct * circ;
    const off = circ - cum;
    cum += len;
    return { ...seg, len, off };
  });
  return (
    <div className="flex items-center gap-5">
      <svg viewBox={`0 0 ${size} ${size}`} width="80" height="80" aria-hidden style={{ transform: 'rotate(-90deg)', flexShrink: 0 }}>
        <circle cx={c} cy={c} r={r} fill="none" stroke="var(--rule)" strokeWidth={sw} />
        {arcs.map(arc => (
          <circle key={arc.key} cx={c} cy={c} r={r} fill="none" strokeWidth={sw}
            strokeLinecap="butt"
            strokeDasharray={`${arc.len} ${circ - arc.len}`}
            strokeDashoffset={arc.off}
            style={{ stroke: CLASS_COLORS[arc.key] ?? 'var(--muted)' }} />
        ))}
      </svg>
      <div className="space-y-1.5">
        {valid.map(seg => (
          <div key={seg.key} className="flex items-center gap-2">
            <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ background: CLASS_COLORS[seg.key] ?? 'var(--muted)' }} />
            <span className="text-xs text-ghost">{seg.label}</span>
            <span className="text-xs font-semibold text-dim tabular-nums">{Math.round(seg.pct * 100)}%</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── stacked allocation bar ────────────────────────────────────────────────────

function AllocationBar({ classes }: { classes: AssetClassItem[] }) {
  const total = classes.reduce((s, c) => s + c.value, 0);
  if (total <= 0) return null;
  return (
    <div className="flex rounded-full overflow-hidden h-2 w-full gap-px">
      {classes.map(c => (
        <div key={c.asset_class}
          style={{ width: `${(c.value / total) * 100}%`, background: CLASS_COLORS[c.asset_class] ?? 'var(--muted)' }}
          title={`${CLASS_LABELS[c.asset_class] ?? c.asset_class}: ${Math.round(c.pct)}%`}
        />
      ))}
    </div>
  );
}

// ── stat cell ─────────────────────────────────────────────────────────────────

function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div>
      <p className="text-[11px] text-ghost mb-0.5">{label}</p>
      <p className="text-sm font-semibold tabular-nums" style={{ color: color ?? 'var(--ink)' }}>{value}</p>
    </div>
  );
}

// ── entity card ───────────────────────────────────────────────────────────────

function EntityCard({ entity }: { entity: EntitySummary }) {
  const pnlColor = gainColor(entity.total_pnl);
  return (
    <div className="bg-card rounded-lg border border-rule p-5">
      <div className="flex items-start justify-between gap-3 mb-3">
        <div>
          <h3 className="text-sm font-semibold text-ink">{entity.entity_name}</h3>
          <p className="text-xs text-ghost mt-0.5">Portfolio</p>
        </div>
        <div className="text-right">
          <p className="text-base font-bold text-ink tabular-nums">{fmtINRCompact(entity.total_value)}</p>
          <p className="text-[11px] mt-0.5 tabular-nums" style={{ color: pnlColor }}>
            {entity.total_pnl >= 0 ? '+' : ''}{fmtINRCompact(entity.total_pnl)}
          </p>
          <p className="text-[10px] text-ghost mt-0.5">Unrealized profit as of today</p>
        </div>
      </div>

      {/* Allocation bar */}
      <AllocationBar classes={entity.asset_classes} />

      {/* Class breakdown */}
      <div className="mt-3 space-y-1.5">
        {entity.asset_classes.map(cls => (
          <div key={cls.asset_class} className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-1.5 min-w-0">
              <span className="w-2 h-2 rounded-full shrink-0"
                style={{ background: CLASS_COLORS[cls.asset_class] ?? 'var(--muted)' }} />
              <span className="text-[11px] text-ghost truncate">
                {CLASS_LABELS[cls.asset_class] ?? cls.asset_class}
              </span>
            </div>
            <div className="flex items-center gap-3 shrink-0">
              <span className="text-[11px] text-dim tabular-nums w-20 text-right">{fmtINRCompact(cls.value)}</span>
              <span className="text-[11px] font-medium tabular-nums w-20 text-right" style={{ color: gainColor(cls.pnl) }}>
                {cls.pnl >= 0 ? '+' : ''}{fmtINRCompact(cls.pnl)}
              </span>
              <span className="text-[11px] text-ghost w-8 text-right tabular-nums">{cls.pct.toFixed(0)}%</span>
            </div>
          </div>
        ))}
      </div>

      {/* Footer stats */}
      {(entity.total_pnl_ytd !== 0 || entity.total_weekly !== 0) && (
        <div className="mt-3 pt-3 border-t border-rule flex gap-5">
          {entity.total_pnl_ytd !== 0 && (
            <div>
              <p className="text-[10px] text-ghost mb-0.5">P&L YTD</p>
              <p className="text-xs font-medium tabular-nums" style={{ color: gainColor(entity.total_pnl_ytd) }}>
                {entity.total_pnl_ytd >= 0 ? '+' : ''}{fmtINRCompact(entity.total_pnl_ytd)}
              </p>
            </div>
          )}
          {entity.total_weekly !== 0 && (
            <div>
              <p className="text-[10px] text-ghost mb-0.5">This Week</p>
              <p className="text-xs font-medium tabular-nums" style={{ color: gainColor(entity.total_weekly) }}>
                {entity.total_weekly >= 0 ? '+' : ''}{fmtINRCompact(entity.total_weekly)}
              </p>
            </div>
          )}
          <div>
            <p className="text-[10px] text-ghost mb-0.5">Invested</p>
            <p className="text-xs font-medium text-dim tabular-nums">{fmtINRCompact(entity.total_invested)}</p>
          </div>
        </div>
      )}
    </div>
  );
}

// ── include-toggles ───────────────────────────────────────────────────────────
// Property register and Art/Collectibles sit outside the portfolio totals by
// default (they're standalone sheets, not the traded book). These let you fold
// them in for a "everything we own" view without making that the default.
//
// Collapsed unless asked for — same ▸/▾ reveal the properties page uses for its
// Parent Companies group, so this stays out of the way on an ordinary visit.

export interface IncludeOpts { property: boolean; art: boolean; parents: boolean }
const DEFAULT_INCLUDE: IncludeOpts = { property: false, art: false, parents: false };

function IncludeToggles({ value, onChange, busy }: {
  value: IncludeOpts; onChange: (v: IncludeOpts) => void; busy: boolean;
}) {
  const [open, setOpen] = useState(false);
  const activeCount = (value.property ? 1 : 0) + (value.art ? 1 : 0);

  const pill = (on: boolean) =>
    `shrink-0 px-3 py-1.5 rounded text-xs font-medium transition-colors border ${
      on ? 'bg-prime text-prime-fg border-prime'
         : 'bg-card border-rule text-dim hover:border-dim hover:text-ink'}`;

  return (
    <div className="mb-5">
      <button
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
        className="inline-flex items-center gap-1.5 text-xs text-dim hover:text-ink transition-colors"
      >
        <span aria-hidden>{open ? '▾' : '▸'}</span>
        Include more
        {activeCount > 0 && (
          <span className="text-[10px] px-1.5 py-px rounded bg-prime/10 text-prime">{activeCount}</span>
        )}
        {busy && <span className="text-[10px] text-ghost">updating…</span>}
      </button>

      {open && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <button className={pill(value.property)}
                  aria-pressed={value.property}
                  onClick={() => onChange({ ...value, property: !value.property })}>
            Properties
          </button>
          <button className={pill(value.art)}
                  aria-pressed={value.art}
                  onClick={() => onChange({ ...value, art: !value.art })}>
            Art
          </button>
          {/* Only meaningful once Properties is on — the backend ignores it otherwise. */}
          {value.property && (
            <>
              <span className="text-ghost text-xs px-1" aria-hidden>·</span>
              <button className={pill(value.parents)}
                      aria-pressed={value.parents}
                      onClick={() => onChange({ ...value, parents: !value.parents })}>
                incl. parent companies
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}

// ── overview section ──────────────────────────────────────────────────────────

function OverviewSection({ data, include, onInclude, ovBusy }: {
  data: OverviewData; include: IncludeOpts;
  onInclude: (v: IncludeOpts) => void; ovBusy: boolean;
}) {
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const toggleEntity = useCallback((id: number | null) => {
    if (id === null) { setSelectedIds([]); return; }
    setSelectedIds(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);
  }, []);

  const allEntities = data.entities;
  const entityPills = useMemo(
    () => allEntities.map(e => ({ id: e.entity_id, name: e.entity_name })),
    [allEntities],
  );

  // When a subset is chosen, re-aggregate the top summary, donut and asset-class
  // breakdown from just those entities. No selection = the server's full-book view
  // (which also carries a real weighted CAGR we can't recompute per-subset).
  const selKey = selectedIds.join(',');
  const { summary, asset_class_breakdown, entities } = useMemo(() => {
    if (selectedIds.length === 0) return data;
    const chosen = allEntities.filter(e => selectedIds.includes(e.entity_id));
    const s = { total_invested: 0, total_value: 0, total_pnl: 0, total_pnl_ytd: 0, total_weekly: 0, weighted_cagr: null as number | null };
    const byClass = new Map<string, AssetClassItem>();
    for (const e of chosen) {
      s.total_invested += e.total_invested;
      s.total_value    += e.total_value;
      s.total_pnl      += e.total_pnl;
      s.total_pnl_ytd  += e.total_pnl_ytd;
      s.total_weekly   += e.total_weekly;
      for (const c of e.asset_classes) {
        const agg = byClass.get(c.asset_class) ?? { asset_class: c.asset_class, invested: 0, value: 0, pnl: 0, pct: 0 };
        agg.invested += c.invested;
        agg.value    += c.value;
        agg.pnl      += c.pnl;
        byClass.set(c.asset_class, agg);
      }
    }
    const breakdown = Array.from(byClass.values())
      .map(c => ({ ...c, pct: s.total_value > 0 ? (c.value / s.total_value) * 100 : 0 }))
      .sort((a, b) => b.value - a.value);
    return { summary: s, asset_class_breakdown: breakdown, entities: chosen };
  }, [data, allEntities, selKey]);   // eslint-disable-line react-hooks/exhaustive-deps

  const pnlColor   = gainColor(summary.total_pnl);
  const ytdColor   = gainColor(summary.total_pnl_ytd);
  const weekColor  = gainColor(summary.total_weekly);
  const cagrColor  = summary.weighted_cagr != null ? gainColor(summary.weighted_cagr) : 'var(--ink)';

  const donutSegments = asset_class_breakdown.map(c => ({
    key:   c.asset_class,
    label: CLASS_LABELS[c.asset_class] ?? c.asset_class,
    value: c.value,
    pct:   c.pct / 100,
  }));

  return (
    <div className="space-y-6">

      {/* Multi-entity filter — view the whole book or an arbitrary subset */}
      {entityPills.length > 1 && (
        <EntitySwitcher entities={entityPills} selectedIds={selectedIds} onToggle={toggleEntity} />
      )}

      {/* Fold in the standalone sheets (property register / art) — collapsed by default */}
      <IncludeToggles value={include} onChange={onInclude} busy={ovBusy} />

      {/* Portfolio summary card */}
      <div className="bg-card rounded-lg border border-rule p-5 sm:p-6">
        <h2 className="text-base font-semibold text-ink mb-5">Portfolio Overview</h2>

        <div className="flex flex-wrap items-center gap-8 mb-6">
          {/* Big value */}
          <div>
            <p className="text-xs text-ghost mb-1">Total Value</p>
            <p className="text-3xl font-bold text-ink tabular-nums">{fmtINRCompact(summary.total_value)}</p>
          </div>

          {/* Donut */}
          <DonutChart segments={donutSegments} />
        </div>

        {/* Stats row */}
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-5">
          <Stat label="Invested" value={fmtINRCompact(summary.total_invested)} />
          <Stat label="P&L (Inception)" value={(summary.total_pnl >= 0 ? '+' : '') + fmtINRCompact(summary.total_pnl)} color={pnlColor} />
          <Stat label="P&L YTD" value={summary.total_pnl_ytd !== 0 ? (summary.total_pnl_ytd >= 0 ? '+' : '') + fmtINRCompact(summary.total_pnl_ytd) : '—'} color={ytdColor} />
          <Stat label="This Week" value={summary.total_weekly !== 0 ? (summary.total_weekly >= 0 ? '+' : '') + fmtINRCompact(summary.total_weekly) : '—'} color={weekColor} />
          <Stat label="Avg CAGR" value={fmtPct(summary.weighted_cagr) + (summary.weighted_cagr != null ? ' p.a.' : '')} color={cagrColor} />
          <Stat label="Entities" value={entities.length.toString()} />
        </div>

        {/* Asset class breakdown table */}
        {asset_class_breakdown.length > 0 && (
          <div className="mt-6 pt-5 border-t border-rule">
            <p className="text-[11px] text-ghost font-medium mb-3 uppercase tracking-wide">Asset Class Breakdown</p>
            <div className="space-y-2">
              {asset_class_breakdown.map(c => (
                <div key={c.asset_class} className="flex items-center gap-3">
                  <div className="flex items-center gap-2 w-36 shrink-0">
                    <span className="w-2 h-2 rounded-full shrink-0" style={{ background: CLASS_COLORS[c.asset_class] ?? 'var(--muted)' }} />
                    <span className="text-xs text-ghost truncate">{CLASS_LABELS[c.asset_class] ?? c.asset_class}</span>
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="h-1.5 rounded-full overflow-hidden bg-wire">
                      <div className="h-full w-full rounded-full" style={{ transform: `scaleX(${Math.max(0, Math.min(c.pct, 100)) / 100})`, transformOrigin: 'left center', background: CLASS_COLORS[c.asset_class] ?? 'var(--muted)', transition: 'transform 400ms cubic-bezier(0.22, 1, 0.36, 1)' }} />
                    </div>
                  </div>
                  <span className="text-[11px] text-ghost w-8 text-right tabular-nums shrink-0">{c.pct.toFixed(0)}%</span>
                  <span className="text-xs font-medium text-dim tabular-nums w-28 text-right shrink-0">{fmtINRCompact(c.value)}</span>
                  <span className="text-xs font-medium tabular-nums w-24 text-right shrink-0" style={{ color: gainColor(c.pnl) }}>
                    {c.pnl >= 0 ? '+' : ''}{fmtINRCompact(c.pnl)}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Entity cards */}
      <div>
        <h2 className="text-sm font-semibold text-ink mb-3">By Entity</h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-4">
          {entities.map(e => <EntityCard key={e.entity_id} entity={e} />)}
        </div>
      </div>

    </div>
  );
}

// ── dashboard page ────────────────────────────────────────────────────────────

export default function DashboardPage() {
  const [user, setUser]           = useState<User | null>(null);
  const [overview, setOverview]   = useState<OverviewData | null>(null);
  const [loading, setLoading]     = useState(true);
  const [error, setError]         = useState<string | null>(null);
  const [include, setInclude]     = useState<IncludeOpts>(DEFAULT_INCLUDE);
  const [ovBusy, setOvBusy]       = useState(false);
  const router = useRouter();

  // Sign-out and idle-timeout both used to be duplicated here; they now live in
  // the global TopBar and IdleTimeout components mounted in the root layout.

  // Session — fetched once. Kept separate from the overview so flipping an
  // include-toggle doesn't re-check the session on every click.
  useEffect(() => {
    const ctrl = new AbortController();
    fetch(`${API_URL}/api/v1/me`, { credentials: 'include', signal: ctrl.signal })
      .then(async res => {
        if (res.status === 401) { router.push('/'); return; }
        if (!res.ok) throw new Error('Unable to load session.');
        setUser(await res.json());
      })
      .catch(err => { if (err.name !== 'AbortError') setError(err.message); });
    return () => ctrl.abort();
  }, [router]);

  // Overview — refetched whenever the include-toggles change. Property and Art
  // totals are computed server-side (the client only re-aggregates entity
  // subsets), so these have to be a round trip, not a local filter.
  const incKey = `${include.property}|${include.art}|${include.parents}`;
  useEffect(() => {
    const ctrl = new AbortController();
    setOvBusy(true);
    const qs = new URLSearchParams();
    if (include.property) qs.set('include_property', 'true');
    if (include.art)      qs.set('include_art', 'true');
    if (include.property && include.parents) qs.set('include_parent_properties', 'true');
    const suffix = qs.toString() ? `?${qs}` : '';
    fetch(`${API_URL}/api/v1/overview${suffix}`, { credentials: 'include', signal: ctrl.signal })
      .then(async res => {
        if (res.status === 401) { router.push('/'); return; }
        setOverview(res.ok ? await res.json() : null);
        setLoading(false);
        setOvBusy(false);
      })
      .catch(err => {
        if (err.name === 'AbortError') return;
        setError(err.message);
        setLoading(false);
        setOvBusy(false);
      });
    return () => ctrl.abort();
  }, [router, incKey]);   // eslint-disable-line react-hooks/exhaustive-deps

  // ── render ──────────────────────────────────────────────────────────────────

  return (
    <div className="min-h-screen bg-page">
      {/* Header */}
      <header className="sticky top-0 z-30 bg-card border-b border-rule">
        <div className="max-w-screen-xl mx-auto px-4 sm:px-6 h-14 flex items-center justify-between gap-4">
          <div className="flex items-center gap-3 min-w-0 flex-1">
            <Image
              src="/logo.png"
              alt=""
              width={342}
              height={346}
              priority
              className="h-8 w-auto shrink-0"
            />
            <span className="text-sm font-semibold text-ink">Rajani MIS</span>
            <NavTabs active="/dashboard" role={user?.role} variant="links" className="hidden sm:flex ml-4" />
          </div>
          {/* Identity + Sign out live in the global TopBar (root layout) so they
              are reachable from every page, not just here. */}
        </div>
      </header>

      <main className="max-w-screen-xl mx-auto px-4 sm:px-6 py-6">
        {error && (
          <div className="mb-6 rounded-lg border border-peril/30 bg-peril/5 px-4 py-3 text-sm text-peril">
            {error}
          </div>
        )}

        {loading && (
          <div className="space-y-4">
            <div className="h-64 rounded-lg bg-card border border-rule animate-pulse" />
            <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-4">
              {[1, 2, 3].map(i => (
                <div key={i} className="h-48 rounded-lg bg-card border border-rule animate-pulse" />
              ))}
            </div>
          </div>
        )}

        {/* Content + market rail. The rail is a right-hand column from `lg` up and
            drops below the content on anything narrower — it's supporting context,
            so it should never squeeze the portfolio itself on a small screen. */}
        {!loading && overview && (
          <div className="lg:grid lg:grid-cols-[minmax(0,1fr)_320px] lg:gap-6 lg:items-start">
            <OverviewSection data={overview} include={include} onInclude={setInclude} ovBusy={ovBusy} />
            <div className="mt-6 lg:mt-0 lg:sticky lg:top-16">
              <MarketRail />
            </div>
          </div>
        )}

        {!loading && !overview && !error && (
          <div className="bg-card rounded-lg border border-rule px-6 py-16 text-center">
            <p className="text-sm font-medium text-ink mb-1">No portfolio data yet</p>
            <p className="text-xs text-ghost">Holdings will appear after the first sync.</p>
          </div>
        )}
      </main>
    </div>
  );
}
