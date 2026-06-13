'use client';
import { useEffect, useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';
const IDLE_TIMEOUT = 30 * 60 * 1000;

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
  FIXED_INCOME:  'var(--chart-fixed)',
  ALTERNATES:    'var(--chart-alt)',
  MF:            'var(--chart-equity)',
};

const CLASS_LABELS: Record<string, string> = {
  EQUITY:        'Equity MF',
  DIRECT_EQUITY: 'Direct Equity',
  FIXED_INCOME:  'Fixed Income',
  ALTERNATES:    'Alternates',
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
          <p className="text-[11px] mt-0.5" style={{ color: pnlColor }}>
            {entity.total_pnl >= 0 ? '+' : ''}{fmtINRCompact(entity.total_pnl)} P&L
          </p>
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
              <span className="text-[11px] text-dim tabular-nums">{fmtINRCompact(cls.value)}</span>
              <span className="text-[11px] font-medium" style={{ color: gainColor(cls.pnl) }}>
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

// ── overview section ──────────────────────────────────────────────────────────

function OverviewSection({ data }: { data: OverviewData }) {
  const { summary, asset_class_breakdown, entities } = data;
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
                      <div className="h-full rounded-full transition-all" style={{ width: `${c.pct}%`, background: CLASS_COLORS[c.asset_class] ?? 'var(--muted)' }} />
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
  const [loggingOut, setLoggingOut] = useState(false);
  const router = useRouter();

  const handleLogout = useCallback(async () => {
    setLoggingOut(true);
    try {
      await fetch(`${API_URL}/api/v1/auth/logout`, { method: 'POST', credentials: 'include' });
    } catch { /* ignore */ }
    router.push('/');
  }, [router]);

  // Idle timeout
  useEffect(() => {
    let t: NodeJS.Timeout;
    const reset = () => { clearTimeout(t); t = setTimeout(handleLogout, IDLE_TIMEOUT); };
    const evts = ['mousedown', 'keypress', 'touchstart'] as const;
    evts.forEach(e => window.addEventListener(e, reset));
    window.addEventListener('scroll', reset, { passive: true });
    reset();
    return () => { clearTimeout(t); evts.forEach(e => window.removeEventListener(e, reset)); window.removeEventListener('scroll', reset); };
  }, [handleLogout]);

  // Fetch user + overview in parallel
  useEffect(() => {
    const ctrl = new AbortController();
    Promise.all([
      fetch(`${API_URL}/api/v1/me`,       { credentials: 'include', signal: ctrl.signal }),
      fetch(`${API_URL}/api/v1/overview`, { credentials: 'include', signal: ctrl.signal }),
    ])
      .then(async ([meRes, ovRes]) => {
        if (meRes.status === 401 || ovRes.status === 401) { router.push('/'); return; }
        if (!meRes.ok) throw new Error('Unable to load session.');
        const me  = await meRes.json();
        const ov  = ovRes.ok ? await ovRes.json() : null;
        setUser(me);
        setOverview(ov);
        setLoading(false);
      })
      .catch(err => {
        if (err.name === 'AbortError') return;
        setError(err.message);
        setLoading(false);
      });
    return () => ctrl.abort();
  }, [router]);

  // ── render ──────────────────────────────────────────────────────────────────

  return (
    <div className="min-h-screen bg-page">
      {/* Header */}
      <header className="sticky top-0 z-30 bg-card border-b border-rule">
        <div className="max-w-screen-xl mx-auto px-4 sm:px-6 h-14 flex items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <span className="text-sm font-semibold text-ink">IWS Finserv</span>
            <nav className="hidden sm:flex items-center gap-0.5 ml-4">
              {[
                { href: '/dashboard', label: 'Overview', active: true },
                { href: '/mutual-funds', label: 'Mutual Funds' },
                { href: '/equity', label: 'Equity' },
                { href: '/manual-data', label: 'Manual Data' },
                { href: '/reports', label: 'Reports' },
                { href: '/benchmarks', label: 'Benchmarks' },
                { href: '/realised-gains', label: 'Realised Gains' },
                { href: '/assistant', label: 'Assistant' },
              ].map(link => (
                <a key={link.href} href={link.href}
                  className={`px-3 py-1.5 rounded text-xs font-medium transition-colors ${
                    link.active
                      ? 'bg-prime/10 text-prime'
                      : 'text-dim hover:text-ink hover:bg-page'
                  }`}
                >
                  {link.label}
                </a>
              ))}
            </nav>
          </div>
          <div className="flex items-center gap-3">
            {user && (
              <span className="text-xs text-ghost hidden sm:block">
                {user.full_name || user.email}
              </span>
            )}
            <button
              onClick={handleLogout}
              disabled={loggingOut}
              className="text-xs text-dim hover:text-ink transition-colors disabled:opacity-50"
            >
              {loggingOut ? 'Signing out…' : 'Sign out'}
            </button>
          </div>
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

        {!loading && overview && <OverviewSection data={overview} />}

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
