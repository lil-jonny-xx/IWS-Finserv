'use client';
import { useEffect, useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

interface Benchmark {
  code: string;
  label: string;
  unit: string;
  current: number | null;
  prev_week: number | null;
  mar31: number | null;
  week_pct: number | null;
  ytd_pct: number | null;
}

const NAV = [
  { href: '/dashboard',      label: 'Dashboard' },
  { href: '/mutual-funds',   label: 'Mutual Funds' },
  { href: '/equity',         label: 'Equity' },
  { href: '/foreign-equity', label: 'Foreign Equity' },
  { href: '/bank-accounts', label: 'Banks' },
  { href: '/pms', label: 'PMS' },
  { href: '/manual-data',    label: 'Manual Data' },
  { href: '/reports',        label: 'Reports' },
  { href: '/benchmarks',     label: 'Benchmarks', active: true },
  { href: '/realised-gains', label: 'Realised Gains' },
  { href: '/assistant',      label: 'Assistant' },
];

// GS-bond codes are entered by hand (no live feed).
const MANUAL_CODES = new Set(['GS2032_YTM', 'GS2032_PRICE', 'GS2030_YTM', 'GS2030_PRICE']);

function fmtVal(v: number | null, unit: string): string {
  if (v == null) return '—';
  if (unit === 'pct') return `${(v * 100).toFixed(2)}%`;
  return v.toLocaleString('en-IN', { maximumFractionDigits: 2 });
}
function fmtPct(v: number | null): string {
  if (v == null) return '—';
  return `${(v * 100).toFixed(2)}%`;
}

export default function BenchmarksPage() {
  const router = useRouter();
  const [rows, setRows] = useState<Benchmark[]>([]);
  const [loading, setLoading] = useState(true);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [password, setPassword] = useState('');
  const [msg, setMsg] = useState('');
  const [err, setErr] = useState('');

  const fetchRows = useCallback(async () => {
    const res = await fetch(`${API_URL}/api/v1/benchmarks`, { credentials: 'include' });
    if (res.status === 401) { router.push('/'); return; }
    if (res.ok) setRows(await res.json());
    setLoading(false);
  }, [router]);

  useEffect(() => {
    fetchRows();
    const id = setInterval(fetchRows, 60_000);   // live-refresh every minute
    return () => clearInterval(id);
  }, [fetchRows]);

  const today = new Date().toISOString().slice(0, 10);

  async function saveManual() {
    setErr(''); setMsg('');
    const entries = Object.entries(edits)
      .filter(([, v]) => v !== '')
      .map(([code, v]) => {
        const row = rows.find(r => r.code === code);
        const num = Number(v);
        return { code, label: row?.label, as_of_date: today,
                 value: row?.unit === 'pct' ? num / 100 : num, unit: row?.unit ?? 'price' };
      });
    if (entries.length === 0) { setErr('Nothing to save.'); return; }
    const res = await fetch(`${API_URL}/api/v1/benchmarks`, {
      method: 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password, entries }),
    });
    if (res.ok) {
      setMsg(`Saved ${entries.length} value(s).`); setEdits({}); setPassword('');
      fetchRows(); setTimeout(() => setMsg(''), 4000);
    } else {
      const d = await res.json().catch(() => ({}));
      setErr(d.detail || 'Save failed.');
    }
  }

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

      <main className="max-w-5xl mx-auto px-4 py-6">
        <div className="mb-6">
          <h1 className="text-lg font-bold" style={{ color: 'var(--ink)' }}>Market Benchmarks</h1>
          <p className="text-xs mt-0.5" style={{ color: 'var(--ghost)' }}>
            Nifty &amp; Sensex update automatically; GS-bond YTM/price are entered manually below.
          </p>
        </div>

        {err && <div className="mb-4 px-4 py-2 rounded text-xs" style={{ background: 'var(--peril)', color: 'var(--peril-fg)' }}>{err}</div>}
        {msg && <div className="mb-4 px-4 py-2 rounded text-xs" style={{ background: 'var(--gain)', color: '#fff' }}>{msg}</div>}

        {loading ? (
          <div className="py-16 text-center text-xs" style={{ color: 'var(--ghost)' }}>Loading…</div>
        ) : (
          <div style={{ background: 'var(--card)', border: '1px solid var(--rule)', borderRadius: 10, overflow: 'hidden' }}>
            <table className="w-full text-xs">
              <thead>
                <tr style={{ background: 'var(--page)', color: 'var(--dim)' }}>
                  {['Benchmark', 'Current', 'Prev Week', '31-Mar', 'Week %', 'YTD %', 'Manual entry'].map(h => (
                    <th key={h} className="px-3 py-2 text-left font-semibold">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map(r => {
                  const manual = MANUAL_CODES.has(r.code);
                  return (
                    <tr key={r.code} style={{ borderTop: '1px solid var(--rule)' }}>
                      <td className="px-3 py-2" style={{ color: 'var(--ink)' }}>{r.label}</td>
                      <td className="px-3 py-2 text-right">{fmtVal(r.current, r.unit)}</td>
                      <td className="px-3 py-2 text-right" style={{ color: 'var(--ghost)' }}>{fmtVal(r.prev_week, r.unit)}</td>
                      <td className="px-3 py-2 text-right" style={{ color: 'var(--ghost)' }}>{fmtVal(r.mar31, r.unit)}</td>
                      <td className="px-3 py-2 text-right">{fmtPct(r.week_pct)}</td>
                      <td className="px-3 py-2 text-right">{fmtPct(r.ytd_pct)}</td>
                      <td className="px-3 py-2">
                        {manual && (
                          <input
                            type="number" step="any" placeholder={r.unit === 'pct' ? '% e.g. 7.12' : 'value'}
                            value={edits[r.code] ?? ''}
                            onChange={e => setEdits(s => ({ ...s, [r.code]: e.target.value }))}
                            className="w-28 px-2 py-1 rounded text-xs"
                            style={{ background: 'var(--page)', border: '1px solid var(--rule)', color: 'var(--ink)' }} />
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>

            <div className="px-3 py-3 flex items-center gap-3" style={{ borderTop: '1px solid var(--rule)' }}>
              <input
                type="password" placeholder="Password to save" value={password}
                onChange={e => setPassword(e.target.value)}
                className="px-2 py-1 rounded text-xs"
                style={{ background: 'var(--page)', border: '1px solid var(--rule)', color: 'var(--ink)' }} />
              <button onClick={saveManual}
                      className="px-4 py-1.5 rounded text-xs font-semibold"
                      style={{ background: 'var(--prime)', color: 'var(--prime-fg)' }}>
                Save manual values (as of {today})
              </button>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
