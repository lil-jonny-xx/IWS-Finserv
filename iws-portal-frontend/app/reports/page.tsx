'use client';
import { useEffect, useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

interface Report {
  id: number;
  type: 'individual' | 'combined' | 'master';
  entity_name: string;
  filename: string;
  as_of_date: string;
  generated_at: string;
  generated_by: string | null;
}

function fmtDT(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleString('en-IN', { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function groupByDate(reports: Report[]): Record<string, Report[]> {
  return reports.reduce<Record<string, Report[]>>((acc, r) => {
    (acc[r.as_of_date] ||= []).push(r);
    return acc;
  }, {});
}

export default function ReportsPage() {
  const router = useRouter();
  const [reports, setReports] = useState<Report[]>([]);
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');

  const fetchReports = useCallback(async () => {
    const res = await fetch(`${API_URL}/api/v1/reports`, { credentials: 'include' });
    if (res.status === 401) { router.push('/'); return; }
    if (res.ok) setReports(await res.json());
    setLoading(false);
  }, [router]);

  useEffect(() => { fetchReports(); }, [fetchReports]);

  async function generate() {
    setGenerating(true);
    setError('');
    setSuccess('');

    const res = await fetch(`${API_URL}/api/v1/reports/generate`, {
      method: 'POST',
      credentials: 'include',
    });
    setGenerating(false);

    if (res.ok) {
      const data = await res.json();
      setSuccess(`Generated ${data.generated} reports successfully.`);
      fetchReports();
      setTimeout(() => setSuccess(''), 5000);
    } else {
      const data = await res.json().catch(() => ({}));
      setError(data.detail || 'Report generation failed.');
    }
  }

  async function downloadReport(id: number, filename: string) {
    const res = await fetch(`${API_URL}/api/v1/reports/${id}/download`, { credentials: 'include' });
    if (!res.ok) { setError('Download failed. The file may not exist on disk.'); return; }
    const blob = await res.blob();
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href     = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }

  const grouped = groupByDate(reports);
  const dates   = Object.keys(grouped).sort().reverse();

  return (
    <div className="min-h-screen" style={{ background: 'var(--page)' }}>
      {/* Nav bar */}
      <header style={{ background: 'var(--card)', borderBottom: '1px solid var(--rule)' }}
              className="px-6 py-3 flex items-center justify-between">
        <div className="flex items-center gap-6">
          <span className="font-bold text-sm" style={{ color: 'var(--ink)' }}>IWS MIS</span>
          <nav className="flex gap-4">
            {[
              { href: '/dashboard',    label: 'Dashboard' },
              { href: '/mutual-funds', label: 'Mutual Funds' },
              { href: '/equity',       label: 'Equity' },
              { href: '/foreign-equity', label: 'Foreign Equity' },
              { href: '/gold-silver', label: 'Gold/Silver' },
              { href: '/unlisted', label: 'Unlisted' },
              { href: '/art', label: 'Art' },
              { href: '/properties', label: 'Properties' },
              { href: '/bank-accounts', label: 'Banks' },
              { href: '/pms', label: 'PMS' },
              { href: '/manual-data',  label: 'Manual Data' },
              { href: '/reports',      label: 'Reports', active: true },
              { href: '/benchmarks',   label: 'Benchmarks' },
              { href: '/realised-gains', label: 'Realised Gains' },
              { href: '/assistant',    label: 'Assistant' },
            ].map(link => (
              <a key={link.href} href={link.href}
                 className="text-xs font-medium transition-colors"
                 style={{ color: link.active ? 'var(--prime)' : 'var(--dim)' }}>
                {link.label}
              </a>
            ))}
          </nav>
        </div>
      </header>

      <main id="main-content" className="max-w-5xl mx-auto px-4 py-6">
        <div className="flex items-center justify-between mb-6">
          <div>
            <h1 className="text-lg font-bold" style={{ color: 'var(--ink)' }}>Portfolio Reports</h1>
            <p className="text-xs mt-0.5" style={{ color: 'var(--ghost)' }}>
              Generate the consolidated MIS workbook — one file, with a sheet per PAN group and per entity
            </p>
          </div>
          <button
            onClick={generate}
            disabled={generating}
            className="flex items-center gap-2 px-4 py-2 rounded text-xs font-semibold transition-colors"
            style={{
              background: generating ? 'var(--muted)' : 'var(--prime)',
              color: 'var(--prime-fg)',
              cursor: generating ? 'not-allowed' : 'pointer',
            }}>
            {generating ? (
              <>
                <svg className="animate-spin w-3.5 h-3.5" viewBox="0 0 24 24" fill="none">
                  <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" strokeOpacity=".25"/>
                  <path d="M12 2a10 10 0 0 1 10 10" stroke="currentColor" strokeWidth="4" strokeLinecap="round"/>
                </svg>
                Generating…
              </>
            ) : '⬇ Generate Reports'}
          </button>
        </div>

        {error && (
          <div className="mb-4 px-4 py-2 rounded text-xs" style={{ background: 'var(--peril)', color: 'var(--peril-fg)' }}>
            {error}
          </div>
        )}
        {success && (
          <div className="mb-4 px-4 py-2 rounded text-xs" style={{ background: 'var(--gain)', color: '#fff' }}>
            {success}
          </div>
        )}

        {/* Info callout */}
        <div className="mb-6 px-4 py-3 rounded-lg text-xs"
             style={{ background: 'var(--notice)', border: '1px solid var(--notice-edge)', color: 'var(--notice-dim)' }}>
          <span className="font-semibold" style={{ color: 'var(--notice-ink)' }}>What's included: </span>
          MF holdings & NAV auto-populated from CAS data · Direct equity from broker sync ·
          Manual values (PMS, bank, overseas, gold) from the{' '}
          <a href="/manual-data" style={{ color: 'var(--prime)', textDecoration: 'underline' }}>Manual Data</a> page.
          Each run produces a single consolidated workbook with the shared sheets plus a Weekly Report
          and Realised P&L sheet for every PAN group and entity.
        </div>

        {loading ? (
          <div className="py-16 text-center text-xs" style={{ color: 'var(--ghost)' }}>Loading…</div>
        ) : dates.length === 0 ? (
          <div className="py-16 text-center rounded-xl" style={{ background: 'var(--card)', border: '1px solid var(--rule)' }}>
            <p className="text-sm font-medium mb-1" style={{ color: 'var(--dim)' }}>No reports yet</p>
            <p className="text-xs" style={{ color: 'var(--ghost)' }}>Click "Generate Reports" to create your first batch.</p>
          </div>
        ) : (
          <div className="space-y-6">
            {dates.map(d => {
              const reps = grouped[d];
              // 'master' = the single consolidated workbook; show it like the combined download.
              const combined   = reps.filter(r => r.type === 'combined' || r.type === 'master');
              const individual = reps.filter(r => r.type === 'individual');

              return (
                <div key={d} style={{ background: 'var(--card)', border: '1px solid var(--rule)', borderRadius: 10, overflow: 'hidden' }}>
                  {/* Date header */}
                  <div className="px-4 py-3 flex items-center justify-between"
                       style={{ borderBottom: '1px solid var(--rule)', background: 'var(--page)' }}>
                    <div>
                      <span className="text-sm font-semibold" style={{ color: 'var(--ink)' }}>
                        {new Date(d + 'T00:00:00').toLocaleDateString('en-IN', { day: '2-digit', month: 'long', year: 'numeric' })}
                      </span>
                      <span className="ml-3 text-xs" style={{ color: 'var(--ghost)' }}>
                        {reps.length} file{reps.length !== 1 ? 's' : ''} · generated {fmtDT(reps[0].generated_at)}
                        {reps[0].generated_by ? ` by ${reps[0].generated_by}` : ''}
                      </span>
                    </div>
                    {/* Download all combined */}
                    {combined.length > 0 && (
                      <button
                        onClick={() => downloadReport(combined[0].id, combined[0].filename)}
                        className="px-3 py-1 rounded text-xs font-medium transition-colors"
                        style={{ background: 'var(--prime)', color: 'var(--prime-fg)' }}>
                        {combined[0].type === 'master' ? '⬇ MIS Report' : '⬇ Combined Report'}
                      </button>
                    )}
                  </div>

                  {/* Individual entity reports */}
                  <div className="divide-y" style={{ borderColor: 'var(--rule)' }}>
                    {individual.map(r => (
                      <div key={r.id} className="px-4 py-2.5 flex items-center justify-between">
                        <div className="flex items-center gap-3">
                          <span className="inline-block px-2 py-0.5 rounded text-xs font-medium"
                                style={{ background: 'var(--page)', border: '1px solid var(--rule)', color: 'var(--dim)' }}>
                            {r.entity_name}
                          </span>
                          <span className="text-xs" style={{ color: 'var(--ghost)' }}>{r.filename}</span>
                        </div>
                        <button
                          onClick={() => downloadReport(r.id, r.filename)}
                          className="px-2.5 py-1 rounded text-xs font-medium transition-colors"
                          style={{ background: 'var(--page)', border: '1px solid var(--rule)', color: 'var(--dim)' }}>
                          ⬇ Download
                        </button>
                      </div>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </main>
    </div>
  );
}
