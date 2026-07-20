'use client';
import { useEffect, useState, useCallback, useRef } from 'react';
import { useRouter } from 'next/navigation';
import EntitySwitcher from '@/app/components/EntitySwitcher';
import { asOf, asOfDate } from '@/app/lib/asOf';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

interface User { role: string; full_name: string; entity_id?: number; }
interface Entity { id: number; name: string; }

interface Attachment {
  id: number; kind: string; original_name: string | null; mime: string | null;
  size_bytes: number | null; has_thumb: boolean;
}
interface BankAsset {
  entity_id: number; entity_name: string; label: string;
  cost: number | null; current_value: number | null; currency: string;
  raw_amount?: number | null;   // balance as entered, in the account's own currency
  fx_rate?: number | null;      // INR-per-unit rate used at entry time
  inception_date: string | null; notes: string | null;
  updated_at?: string | null;   // last time this balance was entered — see lib/asOf
  attachments: Attachment[];
  source?: 'bank' | 'forex';   // which Manual Data category this row came from
}
interface ManualAssetsResponse {
  category: string; entity_id: number; total_value: number; count: number; assets: BankAsset[];
  fx_rates?: Record<string, number>;   // latest INR-per-unit rate per currency (INR = 1)
}

function fmtINR(n: number | null | undefined): string {
  if (n == null) return '—';
  return '₹' + Math.round(n).toLocaleString('en-IN');
}

const CCY_SYMBOL: Record<string, string> = {
  INR: '₹', USD: '$', EUR: '€', GBP: '£', CHF: 'CHF ', SGD: 'S$', AED: 'AED ', HKD: 'HK$',
};
function fmtMoney(n: number | null | undefined, ccy: string): string {
  if (n == null) return '—';
  if (ccy === 'INR') return fmtINR(n);
  const sym = CCY_SYMBOL[ccy] ?? ccy + ' ';
  return (n < 0 ? '−' : '') + sym +
    Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// current_value is always stored in INR; raw_amount (when present) is the exact
// figure entered in the account's own currency. Anything else is derived from
// the latest fx rate and flagged approximate.
function convert(a: BankAsset, ccy: string, fxRates: Record<string, number>):
  { val: number | null; ccy: string; approx: boolean } {
  if (ccy === 'INR') return { val: a.current_value, ccy, approx: false };
  if (ccy === (a.currency || 'INR') && a.raw_amount != null) return { val: a.raw_amount, ccy, approx: false };
  const rate = fxRates[ccy];
  if (a.current_value != null && rate) return { val: a.current_value / rate, ccy, approx: true };
  return { val: a.current_value, ccy: 'INR', approx: false };
}
function fileUrl(id: number)  { return `${API_URL}/api/v1/manual-attachments/${id}/file`; }
function thumbUrl(id: number) { return `${API_URL}/api/v1/manual-attachments/${id}/thumb`; }


function BankCard({ a, showEntity, onOpen, fxRates }: {
  a: BankAsset; showEntity: boolean; onOpen: (id: number) => void;
  fxRates: Record<string, number>;
}) {
  const images = a.attachments.filter(t => (t.mime || '').startsWith('image/'));
  const files  = a.attachments.filter(t => !(t.mime || '').startsWith('image/'));
  const native = a.currency || 'INR';
  const [ccy, setCcy] = useState(native);
  const ccyOptions = [native, ...Object.keys(fxRates).filter(c => c !== native).sort()];
  const { val, ccy: shownCcy, approx } = convert(a, ccy, fxRates);
  const entered = asOf(a.updated_at);
  return (
    <div className="bg-card rounded-lg border border-rule overflow-hidden flex flex-col">
      <div className="px-5 pt-4 pb-3 border-b border-rule flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-ink leading-tight">{a.label}</h3>
          {showEntity && <p className="text-[11px] text-ghost mt-0.5">{a.entity_name}</p>}
          {a.inception_date && <p className="text-[11px] text-ghost">As of {a.inception_date}</p>}
          {entered && (
            <p className="text-[11px] mt-0.5"
               style={{ color: entered.stale ? 'var(--caution)' : 'var(--ghost)' }}
               title={`Balance last entered on ${asOfDate(a.updated_at)}`}>
              {entered.stale && '⚠ '}Entered {entered.label}
            </p>
          )}
        </div>
        <div className="text-right shrink-0">
          <p className="text-[11px] uppercase tracking-wide text-ghost">Balance</p>
          <p className="text-base font-bold text-ink tabular-nums">
            {approx && <span className="font-normal text-ghost">≈ </span>}{fmtMoney(val, shownCcy)}
          </p>
          {ccyOptions.length > 1 ? (
            <select value={ccy} onChange={e => setCcy(e.target.value)} aria-label="Display currency"
              className="mt-0.5 text-[11px] text-ghost bg-transparent border-0 p-0 cursor-pointer hover:text-ink focus:outline-none focus:text-ink transition-colors text-right">
              {ccyOptions.map(c => (
                <option key={c} value={c}>{c === native ? `${c} · native` : c}</option>
              ))}
            </select>
          ) : native !== 'INR' ? (
            <p className="text-[11px] text-ghost">{native} account</p>
          ) : null}
        </div>
      </div>
      <div className="p-4 flex flex-col gap-3 flex-1">
        {images.length > 0 && (
          <div className="grid grid-cols-3 gap-2">
            {images.map(im => (
              <button key={im.id} onClick={() => onOpen(im.id)} className="aspect-square bg-page rounded overflow-hidden" aria-label={`View ${im.original_name || 'statement'}`}>
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={thumbUrl(im.id)} alt={im.original_name || 'statement'} loading="lazy" className="w-full h-full object-cover hover:scale-[1.03] transition-transform" />
              </button>
            ))}
          </div>
        )}
        {files.length > 0 ? (
          <ul className="flex flex-col gap-1.5">
            {files.map(d => (
              <li key={d.id}>
                <a href={fileUrl(d.id)} target="_blank" rel="noopener noreferrer"
                   className="flex items-center justify-between gap-2 text-xs px-2.5 py-1.5 rounded border border-rule text-dim hover:text-ink hover:border-dim transition-colors">
                  <span className="truncate">📄 {d.original_name || 'statement'}</span>
                  <span className="text-[10px] uppercase tracking-wide text-ghost shrink-0">Statement</span>
                </a>
              </li>
            ))}
          </ul>
        ) : images.length === 0 ? (
          <p className="text-xs text-ghost">No statements uploaded.</p>
        ) : null}
        {a.notes && <p className="text-[11px] text-ghost border-t border-rule pt-2">{a.notes}</p>}
      </div>
    </div>
  );
}

export default function BanksPage() {
  const router = useRouter();
  const [user, setUser]             = useState<User | null>(null);
  const [entities, setEntities]     = useState<Entity[]>([]);
  const [selectedIds, setSelectedIds] = useState<number[]>([]);   // empty = All; >1 = subset
  const selKey = selectedIds.join(',');
  const toggleEntity = useCallback((id: number | null) => {
    if (id === null) { setSelectedIds([]); return; }
    setSelectedIds(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);
  }, []);
  const [data, setData]             = useState<ManualAssetsResponse | null>(null);
  const [loading, setLoading]       = useState(true);
  const [error, setError]           = useState<string | null>(null);
  const [retryCount, setRetryCount] = useState(0);
  const [lightbox, setLightbox]     = useState<number | null>(null);
  const didInitialLoad              = useRef(false);

  useEffect(() => {
    fetch(`${API_URL}/api/v1/me`, { credentials: 'include' })
      .then(r => { if (r.status === 401) { router.push('/'); return null; } return r.json(); })
      .then((u: User | null) => {
        if (!u) return;
        setUser(u);
        if (u) {
          fetch(`${API_URL}/api/v1/entities`, { credentials: 'include' })
            .then(r => r.ok ? r.json() : []).then((e: Entity[]) => setEntities(e)).catch(() => {});
        }
      })
      .catch(() => router.push('/'));
  }, [router]);

  useEffect(() => {
    const controller = new AbortController();
    if (!didInitialLoad.current) setLoading(true);
    setError(null);
    const qs = selectedIds.length ? '&' + selectedIds.map(id => `entity_id=${id}`).join('&') : '';
    // Bank Balance + Forex/Foreign-cash rows both live in Manual Data; pull both and
    // merge into one view. Forex balances roll into the headline total below.
    const load = (cat: string) =>
      fetch(`${API_URL}/api/v1/manual-assets?category=${cat}${qs}`, { credentials: 'include', signal: controller.signal })
        .then(r => {
          if (r.status === 401) { router.push('/'); return null; }
          if (!r.ok) throw new Error('Failed to load bank balances.');
          return r.json() as Promise<ManualAssetsResponse>;
        });
    Promise.all([load('bank'), load('forex')])
      .then(([bank, forex]) => {
        if (!bank && !forex) return;   // 401 already redirected
        const tag = (resp: ManualAssetsResponse | null, source: 'bank' | 'forex'): BankAsset[] =>
          (resp?.assets ?? []).map(a => ({ ...a, source }));
        setData({
          category:    'bank',
          entity_id:   bank?.entity_id ?? forex?.entity_id ?? 0,
          total_value: (bank?.total_value ?? 0) + (forex?.total_value ?? 0),
          count:       (bank?.count ?? 0) + (forex?.count ?? 0),
          assets:      [...tag(bank, 'bank'), ...tag(forex, 'forex')],
          fx_rates:    bank?.fx_rates ?? forex?.fx_rates,
        });
        setLoading(false);
        didInitialLoad.current = true;
      })
      .catch(err => { if (err.name !== 'AbortError') { setError(err.message); setLoading(false); } });
    return () => controller.abort();
  }, [router, selKey, retryCount]);   // eslint-disable-line react-hooks/exhaustive-deps

  const isAdmin     = !!user;  // members have admin-level view access (only Manual Data + user mgmt are admin-only)
  const showEntity  = isAdmin && selectedIds.length !== 1;
  const handleRetry = useCallback(() => setRetryCount(c => c + 1), []);

  return (
    <main id="main-content" className="min-h-screen bg-page p-4 sm:p-8">
      <div className="max-w-screen-2xl mx-auto">
        <div className="flex flex-wrap justify-between items-start gap-3 mb-6">
          <div>
            <h1 className="text-2xl sm:text-3xl font-bold text-ink">Bank Accounts</h1>
            <span className="text-sm text-ghost">Bank &amp; forex balances — entered in Manual Data, with uploaded statements</span>
          </div>
        </div>

        {isAdmin && entities.length > 0 && (
          <EntitySwitcher section="/bank-accounts" entities={entities} selectedIds={selectedIds} onToggle={toggleEntity} />
        )}

        {loading && !data && (
          <div className="bg-card rounded-lg border border-rule px-5 py-16 text-center text-sm text-ghost">Loading…</div>
        )}

        {error && !data && (
          <div role="alert" className="bg-card rounded-lg border border-rule px-5 py-5 flex items-start justify-between gap-4">
            <div>
              <p className="text-sm font-medium text-dim">Could not load bank balances</p>
              <p className="text-xs text-ghost mt-1">{error}</p>
            </div>
            <button onClick={handleRetry} className="shrink-0 text-xs border border-wire text-dim px-3 py-1.5 rounded hover:border-dim hover:text-ink transition-colors">Retry</button>
          </div>
        )}

        {data && (
          <div className="fade-in">
            <div className="bg-card rounded-lg border border-rule px-5 sm:px-6 py-4 mb-6 flex flex-wrap gap-8 items-end">
              <div>
                <p className="text-[11px] uppercase tracking-wide text-ghost">Total Balance</p>
                <p className="text-2xl font-bold text-ink tabular-nums">{fmtINR(data.total_value)}</p>
              </div>
              <div>
                <p className="text-[11px] uppercase tracking-wide text-ghost">Accounts</p>
                <p className="text-base font-semibold text-ink tabular-nums">{data.count}</p>
              </div>
              {isAdmin && (
                <a href="/manual-data" className="ml-auto text-xs font-medium border border-rule text-dim px-3 py-1.5 rounded hover:border-dim hover:text-ink transition-colors">
                  + Add / edit in Manual Data
                </a>
              )}
            </div>

            {data.count === 0 ? (
              <div className="bg-card rounded-lg border border-rule px-5 py-16 text-center text-sm text-ghost">
                No bank or forex balances yet. Add one from the{' '}
                <a href="/manual-data" className="text-prime hover:underline">Manual Data</a> page (category “Bank Balance” or “Forex / Foreign Cash”).
              </div>
            ) : (() => {
              const forex = data.assets.filter(a => a.source === 'forex');
              // Keep the plain single grid when there are no forex rows; only split into
              // labelled groups once both a bank and a forex bucket exist.
              if (forex.length === 0) {
                return (
                  <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                    {data.assets.map(a => (
                      <BankCard key={`${a.entity_id}-${a.label}`} a={a} showEntity={showEntity} onOpen={setLightbox} fxRates={data.fx_rates ?? {}} />
                    ))}
                  </div>
                );
              }
              const groups: { key: 'bank' | 'forex'; title: string }[] = [
                { key: 'bank',  title: 'Bank balances' },
                { key: 'forex', title: 'Forex / Foreign cash' },
              ];
              return (
                <div className="flex flex-col gap-8">
                  {groups.map(g => {
                    const rows = data.assets.filter(a => (a.source ?? 'bank') === g.key);
                    if (rows.length === 0) return null;
                    return (
                      <section key={g.key}>
                        <h2 className="text-[11px] uppercase tracking-wide text-ghost mb-3">{g.title}</h2>
                        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                          {rows.map(a => (
                            <BankCard key={`${g.key}-${a.entity_id}-${a.label}`} a={a} showEntity={showEntity} onOpen={setLightbox} fxRates={data.fx_rates ?? {}} />
                          ))}
                        </div>
                      </section>
                    );
                  })}
                </div>
              );
            })()}
          </div>
        )}

        <p className="text-center text-xs text-ghost mt-8">Rajani MIS &copy; {new Date().getFullYear()}</p>
      </div>

      {lightbox != null && (
        <div className="fixed inset-0 z-50 bg-ink/80 flex items-center justify-center p-6" onClick={() => setLightbox(null)}>
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={fileUrl(lightbox)} alt="Statement" className="max-w-full max-h-full object-contain rounded shadow-2xl" />
        </div>
      )}
    </main>
  );
}
