'use client';
import { useEffect, useState, useCallback, useRef, type ReactNode } from 'react';
import { useRouter } from 'next/navigation';
import NavTabs from '@/app/components/NavTabs';
import EntitySwitcher from '@/app/components/EntitySwitcher';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

interface User { role: string; full_name: string; entity_id?: number; }
interface Entity { id: number; name: string; }
interface Attachment {
  id: number; kind: string; original_name: string | null; mime: string | null;
  size_bytes: number | null; has_thumb: boolean;
}
export interface GalleryAsset {
  entity_id: number; entity_name: string; label: string;
  cost: number | null; current_value: number | null; currency: string;
  inception_date: string | null; notes: string | null;
  painter_name: string | null; painter_about: string | null;
  location: string | null; seller_name: string | null; seller_address: string | null;
  attachments: Attachment[];
}
interface ManualAssetsResponse {
  category: string; entity_id: number; total_value: number; count: number; assets: GalleryAsset[];
}

const KIND_LABELS: Record<string, string> = {
  authentication_certificate: 'Authentication Certificate',
  bill: 'Bill',
  deed: 'Deed',
  plan: 'Plan',
  document: 'Document',
};

function fmtMoney(n: number | null | undefined, ccy = 'INR'): string {
  if (n == null) return '—';
  const sym = ccy === 'INR' ? '₹' : ccy + ' ';
  return sym + Math.round(n).toLocaleString('en-IN');
}
function fileUrl(id: number)  { return `${API_URL}/api/v1/manual-attachments/${id}/file`; }
function thumbUrl(id: number) { return `${API_URL}/api/v1/manual-attachments/${id}/thumb`; }

const isImage = (a: Attachment) => (a.mime || '').startsWith('image/') || a.kind === 'art_image';

// Document link text follows the class/kind (Bill, Authentication Certificate…),
// numbered when several of the same kind exist.
function docName(a: Attachment, sameKind: Attachment[]): string {
  const base = KIND_LABELS[a.kind] || 'Document';
  if (sameKind.length <= 1) return base;
  const idx = sameKind.findIndex(x => x.id === a.id);
  return `${base} ${idx + 1}`;
}

function AssetCard({ a, onOpen }: { a: GalleryAsset; onOpen: () => void }) {
  const images = a.attachments.filter(isImage);
  const cover = images[0] || null;
  return (
    <button onClick={onOpen}
      className="text-left bg-card rounded-lg border border-rule overflow-hidden flex flex-col hover:border-dim transition-colors">
      <div className="w-full aspect-[4/3] bg-page overflow-hidden relative">
        {cover ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={cover.has_thumb ? thumbUrl(cover.id) : fileUrl(cover.id)} alt={a.label} loading="lazy"
               className="w-full h-full object-cover" />
        ) : (
          <div className="w-full h-full flex items-center justify-center text-ghost text-xs">No image</div>
        )}
        {images.length > 1 && (
          <span className="absolute bottom-1.5 right-1.5 text-[10px] bg-ink/60 text-white px-1.5 py-0.5 rounded">
            {images.length} photos
          </span>
        )}
      </div>
      <div className="p-3.5 flex flex-col gap-1 flex-1">
        <div className="flex items-start justify-between gap-2">
          <h3 className="text-sm font-semibold text-ink leading-tight">{a.label}</h3>
          <span className="text-sm font-bold text-ink tabular-nums shrink-0">{fmtMoney(a.current_value, a.currency)}</span>
        </div>
        {a.painter_name && <p className="text-xs text-dim">by {a.painter_name}</p>}
        {a.location && <p className="text-[11px] text-ghost">Kept at {a.location}</p>}
      </div>
    </button>
  );
}

function DetailRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <p className="text-[10px] uppercase tracking-wide text-ghost">{label}</p>
      <p className="text-xs text-ink break-words">{children}</p>
    </div>
  );
}

function AssetModal({ a, showPainter, onClose }: { a: GalleryAsset; showPainter: boolean; onClose: () => void }) {
  const images = a.attachments.filter(isImage);
  const docs = a.attachments.filter(x => !isImage(x));
  const [heroId, setHeroId] = useState<number | null>(images[0]?.id ?? null);
  useEffect(() => { setHeroId(images[0]?.id ?? null); }, [a.label]); // eslint-disable-line react-hooks/exhaustive-deps
  const hero = images.find(i => i.id === heroId) || images[0] || null;

  return (
    <div className="fixed inset-0 z-50 bg-ink/70 flex items-start justify-center overflow-y-auto p-4 sm:p-8" onClick={onClose}>
      <div className="bg-card rounded-lg border border-rule w-full max-w-3xl" onClick={e => e.stopPropagation()} role="dialog" aria-label={a.label}>
        <div className="p-4 sm:p-6">
          <div className="flex items-start justify-between gap-3 mb-3">
            <div>
              <h2 className="text-lg font-bold text-ink">{a.label}</h2>
              {a.painter_name && showPainter && <p className="text-xs text-dim">by {a.painter_name}</p>}
              <p className="text-xs text-ghost">{a.entity_name}</p>
            </div>
            <button onClick={onClose} aria-label="Close" className="text-ghost hover:text-ink text-xl leading-none">×</button>
          </div>

          <div className="w-full aspect-[16/9] bg-page rounded-lg overflow-hidden mb-2 flex items-center justify-center">
            {hero ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={fileUrl(hero.id)} alt={a.label} className="w-full h-full object-contain" />
            ) : (
              <span className="text-ghost text-xs">No images uploaded</span>
            )}
          </div>
          {images.length > 1 && (
            <div className="flex flex-wrap gap-2">
              {images.map(im => (
                <button key={im.id} onClick={() => setHeroId(im.id)}
                        className={`w-16 h-16 rounded overflow-hidden border-2 ${im.id === heroId ? 'border-prime' : 'border-transparent'}`}>
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img src={im.has_thumb ? thumbUrl(im.id) : fileUrl(im.id)} alt="" className="w-full h-full object-cover" />
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="px-4 sm:px-6 pb-5 grid grid-cols-2 sm:grid-cols-3 gap-x-4 gap-y-3 border-t border-rule pt-4">
          {showPainter && <DetailRow label="Painter">{a.painter_name || '—'}</DetailRow>}
          <DetailRow label="Location">{a.location || '—'}</DetailRow>
          <DetailRow label="Acquired">{a.inception_date || '—'}</DetailRow>
          <DetailRow label="Purchase price">{fmtMoney(a.cost, a.currency)}</DetailRow>
          <DetailRow label="Current valuation">{fmtMoney(a.current_value, a.currency)}</DetailRow>
          <DetailRow label="Seller">{a.seller_name || '—'}</DetailRow>
          <DetailRow label="Seller address">{a.seller_address || '—'}</DetailRow>
        </div>

        {(a.painter_about || a.notes) && (
          <div className="px-4 sm:px-6 pb-4">
            {a.painter_about && showPainter && <>
              <p className="text-[10px] uppercase tracking-wide text-ghost">About</p>
              <p className="text-xs text-dim whitespace-pre-wrap mb-2">{a.painter_about}</p>
            </>}
            {a.notes && <>
              <p className="text-[10px] uppercase tracking-wide text-ghost">Notes</p>
              <p className="text-xs text-dim whitespace-pre-wrap">{a.notes}</p>
            </>}
          </div>
        )}

        {docs.length > 0 && (
          <div className="px-4 sm:px-6 pb-5 border-t border-rule pt-3">
            <p className="text-[10px] uppercase tracking-wide text-ghost mb-2">Documents</p>
            <div className="flex flex-wrap gap-2">
              {docs.map(d => {
                const sameKind = docs.filter(x => x.kind === d.kind);
                return (
                  <a key={d.id} href={fileUrl(d.id)} target="_blank" rel="noopener noreferrer"
                     className="text-xs px-2.5 py-1 rounded border border-rule text-prime hover:border-dim hover:underline">
                    {docName(d, sameKind)}
                  </a>
                );
              })}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default function GalleryAssetGrid({ category, title, subtitle, showPainter, emptyText }: {
  category: string; title: string; subtitle: string; showPainter: boolean; emptyText: string;
}) {
  const router = useRouter();
  const [user, setUser]             = useState<User | null>(null);
  const [entities, setEntities]     = useState<Entity[]>([]);
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const selKey = selectedIds.join(',');
  const toggleEntity = useCallback((id: number | null) => {
    if (id === null) { setSelectedIds([]); return; }
    setSelectedIds(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);
  }, []);
  const [data, setData]             = useState<ManualAssetsResponse | null>(null);
  const [loading, setLoading]       = useState(true);
  const [error, setError]           = useState<string | null>(null);
  const [retryCount, setRetryCount] = useState(0);
  const [open, setOpen]             = useState<GalleryAsset | null>(null);
  const didInitialLoad              = useRef(false);

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
    fetch(`${API_URL}/api/v1/manual-assets?category=${category}${qs}`, { credentials: 'include', signal: controller.signal })
      .then(r => { if (r.status === 401) { router.push('/'); return null; } if (!r.ok) throw new Error('Failed to load.'); return r.json(); })
      .then((d: ManualAssetsResponse | null) => { if (d) setData(d); setLoading(false); didInitialLoad.current = true; })
      .catch(err => { if (err.name !== 'AbortError') { setError(err.message); setLoading(false); } });
    return () => controller.abort();
  }, [router, selKey, retryCount, category]);   // eslint-disable-line react-hooks/exhaustive-deps

  const isViewer   = !!user;
  const handleRetry = useCallback(() => setRetryCount(c => c + 1), []);

  return (
    <main id="main-content" className="min-h-screen bg-page p-4 sm:p-8">
      <div className="max-w-screen-2xl mx-auto">
        <div className="flex flex-wrap justify-between items-start gap-3 mb-6">
          <div>
            <h1 className="text-2xl sm:text-3xl font-bold text-ink">{title}</h1>
            <span className="text-sm text-ghost">{subtitle}</span>
          </div>
          <NavTabs active={`/${category === 'art' ? 'art' : 'collectibles'}`} role={user?.role} />
        </div>

        {isViewer && entities.length > 0 && (
          <EntitySwitcher section={`/${category === 'art' ? 'art' : 'collectibles'}`} entities={entities}
            selectedIds={selectedIds} onToggle={toggleEntity} />
        )}

        {loading && !data && (
          <div className="bg-card rounded-lg border border-rule px-5 py-16 text-center text-sm text-ghost">Loading…</div>
        )}

        {error && !data && (
          <div role="alert" className="bg-card rounded-lg border border-rule px-5 py-5 flex items-start justify-between gap-4">
            <div>
              <p className="text-sm font-medium text-dim">Could not load {title.toLowerCase()}</p>
              <p className="text-xs text-ghost mt-1">{error}</p>
            </div>
            <button onClick={handleRetry} className="shrink-0 text-xs border border-wire text-dim px-3 py-1.5 rounded hover:border-dim hover:text-ink transition-colors">Retry</button>
          </div>
        )}

        {data && (
          <div className="fade-in">
            <div className="bg-card rounded-lg border border-rule px-5 sm:px-6 py-4 mb-6 flex flex-wrap gap-8 items-end">
              <div>
                <p className="text-[11px] uppercase tracking-wide text-ghost">Total Value</p>
                <p className="text-2xl font-bold text-ink tabular-nums">{fmtMoney(data.total_value)}</p>
              </div>
              <div>
                <p className="text-[11px] uppercase tracking-wide text-ghost">Pieces</p>
                <p className="text-base font-semibold text-ink tabular-nums">{data.count}</p>
              </div>
            </div>

            {data.count === 0 ? (
              <div className="bg-card rounded-lg border border-rule px-5 py-16 text-center text-sm text-ghost">
                {emptyText}
              </div>
            ) : (
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
                {data.assets.map(a => (
                  <AssetCard key={`${a.entity_id}-${a.label}`} a={a} onOpen={() => setOpen(a)} />
                ))}
              </div>
            )}
          </div>
        )}

        <p className="text-center text-xs text-ghost mt-8">Rajani MIS &copy; {new Date().getFullYear()}</p>
      </div>

      {open && <AssetModal a={open} showPainter={showPainter} onClose={() => setOpen(null)} />}
    </main>
  );
}
