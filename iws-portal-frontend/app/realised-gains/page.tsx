'use client';
import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

interface RealisedRow {
  entity: string;
  group: string;
  security_name: string;
  purchase_amount: number | null;
  sale_date: string;
  sale_amount: number | null;
  pnl: number | null;
  return_pct: number | null;
}

const NAV = [
  { href: '/dashboard',      label: 'Dashboard' },
  { href: '/mutual-funds',   label: 'Mutual Funds' },
  { href: '/equity',         label: 'Equity' },
  { href: '/foreign-equity', label: 'Foreign Equity' },
  { href: '/gold-silver', label: 'Gold/Silver' },
  { href: '/bank-accounts', label: 'Banks' },
  { href: '/pms', label: 'PMS' },
  { href: '/manual-data',    label: 'Manual Data' },
  { href: '/reports',        label: 'Reports' },
  { href: '/benchmarks',     label: 'Benchmarks' },
  { href: '/realised-gains', label: 'Realised Gains', active: true },
  { href: '/assistant',      label: 'Assistant' },
];

function inr(v: number | null): string {
  if (v == null) return '—';
  return v.toLocaleString('en-IN', { maximumFractionDigits: 0 });
}
function pct(v: number | null): string {
  if (v == null) return '—';
  return `${(v * 100).toFixed(2)}%`;
}

function Toggle<T extends string>({ label, value, onChange, options }: {
  label: string;
  value: T;
  onChange: (v: T) => void;
  options: { v: T; label: string }[];
}) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-xs font-medium" style={{ color: 'var(--ghost)' }}>{label}</span>
      <div className="inline-flex rounded-lg overflow-hidden" style={{ border: '1px solid var(--rule)' }}>
        {options.map(o => {
          const active = o.v === value;
          return (
            <button
              key={o.v}
              onClick={() => onChange(o.v)}
              className="px-3 py-1 text-xs font-medium transition-colors"
              style={{
                background: active ? 'var(--prime)' : 'var(--card)',
                color: active ? '#fff' : 'var(--dim)',
              }}
            >{o.label}</button>
          );
        })}
      </div>
    </div>
  );
}

type Period = 'fy' | 'inception';
type Switches = 'include' | 'exclude';

export default function RealisedGainsPage() {
  const router = useRouter();
  const [rows, setRows] = useState<RealisedRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [period, setPeriod] = useState<Period>('fy');
  const [switches, setSwitches] = useState<Switches>('include');

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const res = await fetch(
        `${API_URL}/api/v1/realised-gains?period=${period}&switches=${switches}`,
        { credentials: 'include' },
      );
      if (cancelled) return;
      if (res.status === 401) { router.push('/'); return; }
      if (res.ok) setRows(await res.json());
      setLoading(false);
    })();
    return () => { cancelled = true; };
  }, [router, period, switches]);

  // Show the loading state while a toggle change refetches.
  const changePeriod = (p: Period) => { setLoading(true); setPeriod(p); };
  const changeSwitches = (s: Switches) => { setLoading(true); setSwitches(s); };

  const totalPnl = rows.reduce((s, r) => s + (r.pnl ?? 0), 0);

  return (
    <div className="min-h-screen" style={{ background: 'var(--page)' }}>
      <header style={{ background: 'var(--card)', borderBottom: '1px solid var(--rule)' }}
              className="px-6 py-3 flex items-center justify-between">
        <div className="flex items-center gap-6">
          <span className="font-bold text-sm" style={{ color: 'var(--ink)' }}>IWS MIS</span>
          <nav className="flex gap-4">
            {NAV.map(link => (
              <a key={link.href} href={link.href} className="text-xs font-medium transition-colors"
                 style={{ color: link.active ? 'var(--prime)' : 'var(--dim)' }}>{link.label}</a>
            ))}
          </nav>
        </div>
      </header>

      <main className="max-w-6xl mx-auto px-4 py-6">
        <div className="mb-4 flex items-end justify-between">
          <div>
            <h1 className="text-lg font-bold" style={{ color: 'var(--ink)' }}>
              Realised Gains ({period === 'inception' ? 'since inception' : 'FY to date'})
            </h1>
            <p className="text-xs mt-0.5" style={{ color: 'var(--ghost)' }}>
              MF realised auto-computed from CAS transactions. Equity appears once broker trades are imported.
              {' '}Switches are {switches === 'exclude' ? 'excluded' : 'included'}.
            </p>
          </div>
          <div className="text-right">
            <div className="text-xs" style={{ color: 'var(--ghost)' }}>Total realised P&amp;L</div>
            <div className="text-base font-bold" style={{ color: totalPnl >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
              ₹{inr(totalPnl)}
            </div>
          </div>
        </div>

        <div className="mb-6 flex flex-wrap items-center gap-x-6 gap-y-2">
          <Toggle<Period>
            label="Period"
            value={period}
            onChange={changePeriod}
            options={[{ v: 'fy', label: 'FY to date' }, { v: 'inception', label: 'Since inception' }]}
          />
          <Toggle<Switches>
            label="Switches"
            value={switches}
            onChange={changeSwitches}
            options={[{ v: 'include', label: 'Include' }, { v: 'exclude', label: 'Exclude' }]}
          />
        </div>

        {loading ? (
          <div className="py-16 text-center text-xs" style={{ color: 'var(--ghost)' }}>Loading…</div>
        ) : rows.length === 0 ? (
          <div className="py-16 text-center rounded-xl" style={{ background: 'var(--card)', border: '1px solid var(--rule)' }}>
            <p className="text-sm font-medium mb-1" style={{ color: 'var(--dim)' }}>No realised gains this financial year</p>
            <p className="text-xs" style={{ color: 'var(--ghost)' }}>
              They appear as funds/stocks are sold (or after a tradebook import).
            </p>
          </div>
        ) : (
          <div style={{ background: 'var(--card)', border: '1px solid var(--rule)', borderRadius: 10, overflow: 'hidden' }}>
            <table className="w-full text-xs">
              <thead>
                <tr style={{ background: 'var(--page)', color: 'var(--dim)' }}>
                  {['Entity', 'Group', 'Security', 'Purchase Amt', 'Sale Date', 'Sale Amt', 'P&L', 'Return %'].map(h => (
                    <th key={h} className="px-3 py-2 text-left font-semibold">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={i} style={{ borderTop: '1px solid var(--rule)' }}>
                    <td className="px-3 py-2" style={{ color: 'var(--dim)' }}>{r.entity}</td>
                    <td className="px-3 py-2" style={{ color: 'var(--ghost)' }}>{r.group}</td>
                    <td className="px-3 py-2" style={{ color: 'var(--ink)' }}>{r.security_name}</td>
                    <td className="px-3 py-2 text-right">{inr(r.purchase_amount)}</td>
                    <td className="px-3 py-2">{r.sale_date}</td>
                    <td className="px-3 py-2 text-right">{inr(r.sale_amount)}</td>
                    <td className="px-3 py-2 text-right font-medium"
                        style={{ color: (r.pnl ?? 0) >= 0 ? 'var(--gain)' : 'var(--peril)' }}>{inr(r.pnl)}</td>
                    <td className="px-3 py-2 text-right">{pct(r.return_pct)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </main>
    </div>
  );
}
