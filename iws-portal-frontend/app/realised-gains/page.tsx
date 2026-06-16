'use client';
import { useEffect, useState, useCallback } from 'react';
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

export default function RealisedGainsPage() {
  const router = useRouter();
  const [rows, setRows] = useState<RealisedRow[]>([]);
  const [loading, setLoading] = useState(true);

  const fetchRows = useCallback(async () => {
    const res = await fetch(`${API_URL}/api/v1/realised-gains`, { credentials: 'include' });
    if (res.status === 401) { router.push('/'); return; }
    if (res.ok) setRows(await res.json());
    setLoading(false);
  }, [router]);

  useEffect(() => { fetchRows(); }, [fetchRows]);

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
        <div className="mb-6 flex items-end justify-between">
          <div>
            <h1 className="text-lg font-bold" style={{ color: 'var(--ink)' }}>Realised Gains (FY to date)</h1>
            <p className="text-xs mt-0.5" style={{ color: 'var(--ghost)' }}>
              MF realised auto-computed from CAS transactions. Equity appears once broker trades are imported.
            </p>
          </div>
          <div className="text-right">
            <div className="text-xs" style={{ color: 'var(--ghost)' }}>Total realised P&amp;L</div>
            <div className="text-base font-bold" style={{ color: totalPnl >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
              ₹{inr(totalPnl)}
            </div>
          </div>
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
