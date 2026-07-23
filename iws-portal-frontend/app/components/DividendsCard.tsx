'use client';
// Dividends summary for the current financial year, with a click-through pie that
// shows WHICH holdings the dividends came from. Used on both the Equity page
// (scope="domestic" — Indian scrips credited to the bank) and the Foreign Equity
// page (scope="foreign" — Vested/US holdings). Both are DERIVED figures: ex-date ×
// rate/share from market data × the quantity the ledger says was held (see the
// dividend worker), so they are gross and approximate, never recorded cash.
//
// Entity scope is passed in (the page's EntitySwitcher selection), so the same
// component reads "for every entity" — pick Harsh and the pie is Harsh's sources.
//
// The pie is a hand-rolled SVG donut: no chart library (the CSP blocks external
// scripts, and there is nothing to inline for one small chart).
import { useEffect, useMemo, useState } from 'react';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

interface DivRow {
  entity_id: number;
  entity: string;
  security_name: string;
  amount: number;      // always INR
  currency: string;
  ex_date: string;
  fy: string;
}

// Distinct, theme-neutral slice colors. Beyond this many securities the tail is
// folded into a single "Others" slice, so the legend never runs away.
const SLICE_COLORS = [
  '#3772ff', '#e05c00', '#059669', '#7c3aed', '#d2122e',
  '#0891b2', '#b8860b', '#db2777', '#65a30d', '#6366f1',
];
const OTHERS_COLOR = 'var(--ghost)';
const MAX_SLICES = 9;   // 9 named + 1 "Others"

function fmtINR(n: number): string {
  const abs = Math.round(Math.abs(n));
  const s = abs.toString();
  const last3 = s.slice(-3);
  const rest = s.slice(0, -3);
  const grouped = rest.length ? rest.replace(/\B(?=(\d{2})+(?!\d))/g, ',') + ',' + last3 : last3;
  return (n < 0 ? '−₹' : '₹') + grouped;
}

// Current Indian FY label, e.g. "2026-27" (Apr–Mar).
function currentFY(): string {
  const now = new Date();
  const start = now.getMonth() >= 3 ? now.getFullYear() : now.getFullYear() - 1;
  return `${start}-${String(start + 1).slice(-2)}`;
}

export default function DividendsCard({ scope, entityIds }: {
  scope: 'domestic' | 'foreign';
  entityIds: number[];
}) {
  const [rows, setRows] = useState<DivRow[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [open, setOpen] = useState(false);
  const key = entityIds.join(',');

  useEffect(() => {
    const c = new AbortController();
    setLoaded(false);
    const qs = ['period=fy', `scope=${scope}`, ...entityIds.map(id => `entity_id=${id}`)].join('&');
    fetch(`${API_URL}/api/v1/dividends?${qs}`, { credentials: 'include', signal: c.signal })
      .then(r => (r.ok ? r.json() : null))
      .then((j: { rows?: DivRow[] } | null) => { if (j) setRows(j.rows ?? []); setLoaded(true); })
      .catch(err => { if (err.name !== 'AbortError') setLoaded(true); });
    return () => c.abort();
  }, [scope, key]);

  const total = useMemo(() => rows.reduce((s, r) => s + r.amount, 0), [rows]);

  // Sum by security, largest first, tail folded into "Others".
  const slices = useMemo(() => {
    const m = new Map<string, number>();
    for (const r of rows) m.set(r.security_name, (m.get(r.security_name) ?? 0) + r.amount);
    const all = [...m.entries()].map(([name, amount]) => ({ name, amount }))
      .filter(s => s.amount > 0)
      .sort((a, b) => b.amount - a.amount);
    if (all.length <= MAX_SLICES + 1) {
      return all.map((s, i) => ({ ...s, color: SLICE_COLORS[i % SLICE_COLORS.length] }));
    }
    const head = all.slice(0, MAX_SLICES).map((s, i) => ({ ...s, color: SLICE_COLORS[i] }));
    const rest = all.slice(MAX_SLICES).reduce((s, x) => s + x.amount, 0);
    return [...head, { name: `Others (${all.length - MAX_SLICES})`, amount: rest, color: OTHERS_COLOR }];
  }, [rows]);

  // Nothing to show and nothing loading → render nothing, so the page doesn't carry
  // an empty card for an entity with no dividends.
  if (loaded && rows.length === 0) return null;

  const label = scope === 'foreign' ? 'International dividends' : 'Dividends';

  return (
    <>
      <button
        onClick={() => rows.length && setOpen(true)}
        disabled={!rows.length}
        className="flex items-center justify-between gap-6 w-full sm:w-auto sm:min-w-[300px] text-left bg-card rounded-lg border border-rule px-4 py-3 hover:border-dim transition-colors disabled:cursor-default"
        aria-haspopup="dialog"
      >
        <div>
          <p className="text-xs text-ghost mb-0.5">{label} · FY {currentFY()}</p>
          <p className="text-lg font-bold text-ink tabular-nums">
            {loaded ? fmtINR(total) : '…'}
          </p>
        </div>
        <span className="text-[11px] text-prime whitespace-nowrap shrink-0">
          {rows.length ? 'View sources →' : (loaded ? 'None this year' : '')}
        </span>
      </button>

      {open && (
        <div className="fixed inset-0 z-50 bg-ink/60 flex items-center justify-center p-4"
             onClick={() => setOpen(false)}>
          <div className="bg-card rounded-lg border border-rule w-full max-w-lg p-5"
               onClick={e => e.stopPropagation()} role="dialog" aria-label={`${label} sources`}>
            <div className="flex items-start justify-between gap-3 mb-1">
              <div>
                <h3 className="text-base font-semibold text-ink">{label} — where they came from</h3>
                <p className="text-xs text-ghost">FY {currentFY()} · gross · derived from ex-date × rate × quantity held</p>
              </div>
              <button onClick={() => setOpen(false)} aria-label="Close"
                      className="text-ghost hover:text-ink text-lg leading-none">×</button>
            </div>

            <div className="flex flex-wrap items-center gap-5 mt-4">
              <Donut slices={slices} total={total} />
              <ul className="flex-1 min-w-[180px] space-y-1.5 max-h-64 overflow-y-auto">
                {slices.map(s => (
                  <li key={s.name} className="flex items-center gap-2 text-xs">
                    <span className="w-2.5 h-2.5 rounded-sm shrink-0" style={{ background: s.color }} />
                    <span className="text-dim truncate flex-1">{s.name}</span>
                    <span className="text-ink tabular-nums font-medium">{fmtINR(s.amount)}</span>
                    <span className="text-ghost tabular-nums w-10 text-right">
                      {total ? `${((s.amount / total) * 100).toFixed(0)}%` : '—'}
                    </span>
                  </li>
                ))}
              </ul>
            </div>

            <div className="mt-4 pt-3 border-t border-rule flex items-baseline justify-between">
              <span className="text-xs text-ghost">Total (gross)</span>
              <span className="text-sm font-bold text-ink tabular-nums">{fmtINR(total)}</span>
            </div>
            {scope === 'domestic' ? (
              <p className="text-[11px] text-ghost mt-2">
                Indian dividends are paid straight to the bank, never through the broker. Over
                ₹5,000 a year attracts 10% TDS, so the credited amount is lower.
              </p>
            ) : (
              <p className="text-[11px] text-ghost mt-2">
                Converted to INR at each ex-date&apos;s FX rate. US dividends are typically subject to
                withholding tax, so the credited amount is lower.
              </p>
            )}
          </div>
        </div>
      )}
    </>
  );
}

// SVG donut. Each slice is an arc stroked onto one circle via stroke-dasharray,
// offset by the running total — so no path math, and it scales cleanly.
function Donut({ slices, total }: { slices: { name: string; amount: number; color: string }[]; total: number }) {
  const R = 52, C = 2 * Math.PI * R, size = 132, sw = 26;
  let offset = 0;
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="shrink-0"
         role="img" aria-label="Dividend sources by holding">
      <g transform={`rotate(-90 ${size / 2} ${size / 2})`}>
        {total > 0 ? slices.map(s => {
          const frac = s.amount / total;
          const dash = frac * C;
          const el = (
            <circle key={s.name} cx={size / 2} cy={size / 2} r={R} fill="none"
                    stroke={s.color} strokeWidth={sw}
                    strokeDasharray={`${dash} ${C - dash}`} strokeDashoffset={-offset} />
          );
          offset += dash;
          return el;
        }) : (
          <circle cx={size / 2} cy={size / 2} r={R} fill="none" stroke="var(--rule)" strokeWidth={sw} />
        )}
      </g>
      <text x="50%" y="50%" textAnchor="middle" dominantBaseline="central"
            className="fill-ink" style={{ fontSize: 12, fontWeight: 600 }}>
        {slices.length}
      </text>
      <text x="50%" y="62%" textAnchor="middle" dominantBaseline="central"
            className="fill-ghost" style={{ fontSize: 8 }}>
        holdings
      </text>
    </svg>
  );
}
