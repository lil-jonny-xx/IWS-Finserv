'use client';
import { use, useEffect, useState, useCallback, useRef } from 'react';
import { useRouter } from 'next/navigation';
import { Glass } from '@/app/components/PrivacyGlass';
import EntitySwitcher from '@/app/components/EntitySwitcher';
import { DYNAMIC_CATEGORY_LABELS } from '@/app/lib/manualCategories';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

// Generic read-only page for Manual Data categories that have no dedicated
// section (AIF, PPF, liquid/debt funds, …). NavTabs links here the moment a
// category's first entry exists — see app/lib/manualCategories.ts.

interface User { role: string; full_name: string; entity_id?: number; }
interface Entity { id: number; name: string; }
interface Attachment {
  id: number; kind: string; original_name: string | null; mime: string | null;
  size_bytes: number | null; has_thumb: boolean;
}
interface Asset {
  entity_id: number; entity_name: string; label: string;
  cost: number | null; current_value: number | null; currency: string;
  raw_amount?: number | null; fx_rate?: number | null;
  inception_date: string | null; notes: string | null;
  attachments: Attachment[];
}
interface AssetsResponse {
  category: string; entity_id: number; total_value: number; count: number; assets: Asset[];
}

function fmtINR(n: number | null | undefined): string {
  if (n == null) return '—';
  return (n < 0 ? '−₹' : '₹') + Math.round(Math.abs(n)).toLocaleString('en-IN');
}
function fileUrl(id: number)  { return `${API_URL}/api/v1/manual-attachments/${id}/file`; }
function thumbUrl(id: number) { return `${API_URL}/api/v1/manual-attachments/${id}/thumb`; }

function titleFor(category: string): string {
  return DYNAMIC_CATEGORY_LABELS[category]
    ?? category.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

function AssetCard({ a, showEntity, onOpen }: {
  a: Asset; showEntity: boolean; onOpen: (id: number) => void;
}) {
  const images = a.attachments.filter(t => (t.mime || '').startsWith('image/'));
  const files  = a.attachments.filter(t => !(t.mime || '').startsWith('image/'));
  const pnl    = a.cost != null && a.current_value != null ? a.current_value - a.cost : null;
  const pnlPct = pnl != null && a.cost ? (pnl / a.cost) * 100 : null;
  return (
    <div className="bg-card rounded-lg border border-rule overflow-hidden flex flex-col">
      <div className="px-5 pt-4 pb-3 border-b border-rule flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-ink leading-tight">{a.label}</h3>
          {showEntity && <p className="text-[11px] text-ghost mt-0.5">{a.entity_name}</p>}
          {a.inception_date && <p className="text-[11px] text-ghost">Since {a.inception_date}</p>}
        </div>
        <div className="text-right shrink-0">
          <p className="text-[11px] uppercase tracking-wide text-ghost">Current Value</p>
          <p className="text-base font-bold text-ink tabular-nums">{fmtINR(a.current_value)}</p>
          {a.currency && a.currency !== 'INR' && (
            <p className="text-[11px] text-ghost">{a.currency} asset</p>
          )}
        </div>
      </div>
      <div className="px-5 py-3 flex gap-6 text-xs border-b border-rule">
        <div>
          <p className="text-[11px] uppercase tracking-wide text-ghost">Invested</p>
          <p className="font-semibold text-ink tabular-nums">{fmtINR(a.cost)}</p>
        </div>
        <div>
          <p className="text-[11px] uppercase tracking-wide text-ghost">P&amp;L</p>
          <p className={`font-semibold tabular-nums ${pnl == null ? 'text-ink' : pnl >= 0 ? 'text-gain' : 'text-peril'}`}>
            {pnl == null ? '—' : `${pnl >= 0 ? '+' : ''}${fmtINR(pnl)}`}
            {pnlPct != null && <span className="font-normal text-[11px]"> ({pnlPct >= 0 ? '+' : ''}{pnlPct.toFixed(1)}%)</span>}
          </p>
        </div>
      </div>
      <div className="p-4 flex flex-col gap-3 flex-1">
        {images.length > 0 && (
          <div className="grid grid-cols-3 gap-2">
            {images.map(im => (
              <button key={im.id} onClick={() => onOpen(im.id)} className="aspect-square bg-page rounded overflow-hidden" aria-label={`View ${im.original_name || 'document'}`}>
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={thumbUrl(im.id)} alt={im.original_name || 'document'} loading="lazy" className="w-full h-full object-cover hover:scale-[1.03] transition-transform" />
              </button>
            ))}
          </div>
        )}
        {files.length > 0 && (
          <ul className="flex flex-col gap-1.5">
            {files.map(d => (
              <li key={d.id}>
                <a href={fileUrl(d.id)} target="_blank" rel="noopener noreferrer"
                   className="flex items-center justify-between gap-2 text-xs px-2.5 py-1.5 rounded border border-rule text-dim hover:text-ink hover:border-dim transition-colors">
                  <span className="truncate">📄 {d.original_name || 'document'}</span>
                  <span className="text-[10px] uppercase tracking-wide text-ghost shrink-0">Document</span>
                </a>
              </li>
            ))}
          </ul>
        )}
        {a.notes && <p className="text-[11px] text-ghost">{a.notes}</p>}
      </div>
    </div>
  );
}

export default function ManualAssetPage({ params }: { params: Promise<{ category: string }> }) {
  const { category } = use(params);
  const router = useRouter();
  const [user, setUser]             = useState<User | null>(null);
  const [entities, setEntities]     = useState<Entity[]>([]);
  const [selectedIds, setSelectedIds] = useState<number[]>([]);   // empty = All; >1 = subset
  const selKey = selectedIds.join(',');
  const toggleEntity = useCallback((id: number | null) => {
    if (id === null) { setSelectedIds([]); return; }
    setSelectedIds(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);
  }, []);
  const [data, setData]             = useState<AssetsResponse | null>(null);
  const [loading, setLoading]       = useState(true);
  const [error, setError]           = useState<string | null>(null);
  const [retryCount, setRetryCount] = useState(0);
  const [lightbox, setLightbox]     = useState<number | null>(null);
  const didInitialLoad              = useRef(false);

  const title = titleFor(category);

  useEffect(() => {
    fetch(`${API_URL}/api/v1/me`, { credentials: 'include' })
      .then(r => { if (r.status === 401) { router.push('/'); return null; } return r.json(); })
      .then((u: User | null) => {
        if (!u) return;
        setUser(u);
        fetch(`${API_URL}/api/v1/entities`, { credentials: 'include' })
          .then(r => r.ok ? r.json() : []).then((e: Entity[]) => setEntities(e)).catch(() => {});
      })
      .catch(() => router.push('/'));
  }, [router]);

  useEffect(() => {
    const controller = new AbortController();
    if (!didInitialLoad.current) setLoading(true);
    setError(null);
    const qs = selectedIds.length ? '&' + selectedIds.map(id => `entity_id=${id}`).join('&') : '';
    fetch(`${API_URL}/api/v1/manual-assets?category=${encodeURIComponent(category)}${qs}`, { credentials: 'include', signal: controller.signal })
      .then(r => {
        if (r.status === 401) { router.push('/'); return null; }
        if (!r.ok) throw new Error(`Failed to load ${title}.`);
        return r.json() as Promise<AssetsResponse>;
      })
      .then(d => {
        if (!d) return;
        setData(d);
        setLoading(false);
        didInitialLoad.current = true;
      })
      .catch(err => { if (err.name !== 'AbortError') { setError(err.message); setLoading(false); } });
    return () => controller.abort();
  }, [router, category, title, selKey, retryCount]);   // eslint-disable-line react-hooks/exhaustive-deps

  const isAdmin      = user?.role === 'admin';
  const showEntity   = selectedIds.length !== 1;
  const handleRetry  = useCallback(() => setRetryCount(c => c + 1), []);
  const totalCost    = data?.assets.reduce((s, a) => s + (a.cost ?? 0), 0) ?? 0;
  const hasAnyCost   = (data?.assets ?? []).some(a => a.cost != null);
  const totalPnl     = hasAnyCost && data ? data.total_value - totalCost : null;

  return (
    <main id="main-content" className="min-h-screen bg-page py-4 sm:py-8">
      <div className="shell">
        <div className="flex flex-wrap justify-between items-start gap-3 mb-6">
          <div>
            <h1 className="text-2xl sm:text-3xl font-bold text-ink">{title}</h1>
            <span className="text-sm text-ghost">Entered in Manual Data, with attached documents</span>
          </div>
        </div>

        {entities.length > 0 && (
          <EntitySwitcher category={category} entities={entities} selectedIds={selectedIds} onToggle={toggleEntity} />
        )}

        {loading && !data && (
          <div className="bg-card rounded-lg border border-rule px-5 py-16 text-center text-sm text-ghost">Loading…</div>
        )}

        {error && !data && (
          <div role="alert" className="bg-card rounded-lg border border-rule px-5 py-5 flex items-start justify-between gap-4">
            <div>
              <p className="text-sm font-medium text-dim">Could not load {title}</p>
              <p className="text-xs text-ghost mt-1">{error}</p>
            </div>
            <button onClick={handleRetry} className="shrink-0 text-xs border border-wire text-dim px-3 py-1.5 rounded hover:border-dim hover:text-ink transition-colors">Retry</button>
          </div>
        )}

        {data && (
          <div className="fade-in">
            <Glass className="mb-6">
            <div className="bg-card rounded-lg border border-rule px-5 sm:px-6 py-4 flex flex-wrap gap-8 items-end">
              <div>
                <p className="text-[11px] uppercase tracking-wide text-ghost">Total Value</p>
                <p className="text-2xl font-bold text-ink tabular-nums">{fmtINR(data.total_value)}</p>
              </div>
              {hasAnyCost && (
                <>
                  <div>
                    <p className="text-[11px] uppercase tracking-wide text-ghost">Invested</p>
                    <p className="text-base font-semibold text-ink tabular-nums">{fmtINR(totalCost)}</p>
                  </div>
                  <div>
                    <p className="text-[11px] uppercase tracking-wide text-ghost">P&amp;L</p>
                    <p className={`text-base font-semibold tabular-nums ${totalPnl != null && totalPnl < 0 ? 'text-peril' : 'text-gain'}`}>
                      {totalPnl == null ? '—' : `${totalPnl >= 0 ? '+' : ''}${fmtINR(totalPnl)}`}
                    </p>
                  </div>
                </>
              )}
              <div>
                <p className="text-[11px] uppercase tracking-wide text-ghost">Holdings</p>
                <p className="text-base font-semibold text-ink tabular-nums">{data.count}</p>
              </div>
              {isAdmin && (
                <a href="/manual-data" className="ml-auto text-xs font-medium border border-rule text-dim px-3 py-1.5 rounded hover:border-dim hover:text-ink transition-colors">
                  + Add / edit in Manual Data
                </a>
              )}
            </div>
            </Glass>
            {data.count === 0 ? (
              <div className="bg-card rounded-lg border border-rule px-5 py-16 text-center text-sm text-ghost">
                No {title} entries yet. Add one from the{' '}
                <a href="/manual-data" className="text-prime hover:underline">Manual Data</a> page.
              </div>
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                {data.assets.map(a => (
                  <AssetCard key={`${a.entity_id}-${a.label}`} a={a} showEntity={showEntity} onOpen={setLightbox} />
                ))}
              </div>
            )}
          </div>
        )}

        <p className="text-center text-xs text-ghost mt-8">Rajani MIS &copy; {new Date().getFullYear()}</p>
      </div>

      {lightbox != null && (
        <div className="fixed inset-0 z-50 bg-ink/80 flex items-center justify-center p-6" onClick={() => setLightbox(null)}>
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={fileUrl(lightbox)} alt="Document" className="max-w-full max-h-full object-contain rounded shadow-2xl" />
        </div>
      )}
    </main>
  );
}
