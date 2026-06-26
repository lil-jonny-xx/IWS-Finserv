'use client';
import { useEffect, useState, useCallback, useRef } from 'react';
import { useRouter } from 'next/navigation';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

interface User { role: string; full_name: string; entity_id?: number; }
interface Entity { id: number; name: string; }

interface Attachment {
  id: number; kind: string; original_name: string | null; mime: string | null;
  size_bytes: number | null; has_thumb: boolean;
}
interface PropertyAsset {
  entity_id: number; entity_name: string; label: string;
  cost: number | null; current_value: number | null; currency: string;
  inception_date: string | null; notes: string | null;
  attachments: Attachment[];
}
interface ManualAssetsResponse {
  category: string; entity_id: number; total_value: number; count: number; assets: PropertyAsset[];
}

const KIND_LABEL: Record<string, string> = {
  deed: 'Deed', plan: 'Plan', document: 'Document', art_image: 'Image',
};
function fmtINR(n: number | null | undefined): string {
  if (n == null) return '—';
  return '₹' + Math.round(n).toLocaleString('en-IN');
}
function fileUrl(id: number)  { return `${API_URL}/api/v1/manual-attachments/${id}/file`; }
function thumbUrl(id: number) { return `${API_URL}/api/v1/manual-attachments/${id}/thumb`; }

function EntitySwitcher({ entities, selectedId, onSelect }: {
  entities: Entity[]; selectedId: number | null; onSelect: (id: number | null) => void;
}) {
  return (
    <div className="flex flex-wrap gap-1.5 mb-5" role="tablist" aria-label="Entity filter">
      {[{ id: null, name: 'All' }, ...entities.map(e => ({ id: e.id, name: e.name }))].map(tab => {
        const active = tab.id === selectedId;
        return (
          <button key={tab.id ?? 'all'} role="tab" aria-selected={active}
            onClick={() => onSelect(tab.id ?? null)}
            className={`px-3 py-1 rounded text-xs font-medium transition-colors ${
              active ? 'bg-prime text-prime-fg' : 'bg-card border border-rule text-dim hover:border-dim hover:text-ink'}`}>
            {tab.name}
          </button>
        );
      })}
    </div>
  );
}

function PropertyCard({ a, showEntity, onOpen }: {
  a: PropertyAsset; showEntity: boolean; onOpen: (id: number) => void;
}) {
  const images = a.attachments.filter(t => (t.mime || '').startsWith('image/'));
  const files  = a.attachments.filter(t => !(t.mime || '').startsWith('image/'));
  return (
    <div className="bg-card rounded-lg border border-rule overflow-hidden flex flex-col">
      <div className="px-5 pt-4 pb-3 border-b border-rule flex items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-ink leading-tight">{a.label}</h3>
          {showEntity && <p className="text-[11px] text-ghost mt-0.5">{a.entity_name}</p>}
          {a.inception_date && <p className="text-[11px] text-ghost">Acquired {a.inception_date}</p>}
        </div>
        <div className="text-right shrink-0">
          <p className="text-[11px] uppercase tracking-wide text-ghost">Value</p>
          <p className="text-base font-bold text-ink tabular-nums">{fmtINR(a.current_value)}</p>
          {a.cost != null && <p className="text-[11px] text-ghost">Cost {fmtINR(a.cost)}</p>}
        </div>
      </div>
      <div className="p-4 flex flex-col gap-3 flex-1">
        {images.length > 0 && (
          <div className="grid grid-cols-3 gap-2">
            {images.map(im => (
              <button key={im.id} onClick={() => onOpen(im.id)} className="aspect-square bg-page rounded overflow-hidden" aria-label={`View ${im.original_name || 'image'}`}>
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={thumbUrl(im.id)} alt={im.original_name || 'image'} loading="lazy" className="w-full h-full object-cover hover:scale-[1.03] transition-transform" />
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
                  <span className="truncate">📄 {d.original_name || 'document'}</span>
                  <span className="text-[10px] uppercase tracking-wide text-ghost shrink-0">{KIND_LABEL[d.kind] || 'Doc'}</span>
                </a>
              </li>
            ))}
          </ul>
        ) : images.length === 0 ? (
          <p className="text-xs text-ghost">No documents uploaded.</p>
        ) : null}
        {a.notes && <p className="text-[11px] text-ghost border-t border-rule pt-2">{a.notes}</p>}
      </div>
    </div>
  );
}

const NAV = [
  { href: '/dashboard', label: 'Overview' },
  { href: '/mutual-funds', label: 'Mutual Funds' },
  { href: '/equity', label: 'Equity' },
  { href: '/foreign-equity', label: 'Foreign Equity' },
  { href: '/gold-silver', label: 'Gold/Silver' },
  { href: '/unlisted', label: 'Unlisted' },
  { href: '/art', label: 'Art' },
  { href: '/properties', label: 'Properties' },
  { href: '/bank-accounts', label: 'Banks' },
  { href: '/pms', label: 'PMS' },
  { href: '/manual-data', label: 'Manual Data' },
  { href: '/reports', label: 'Reports' },
  { href: '/benchmarks', label: 'Benchmarks' },
  { href: '/realised-gains', label: 'Realised Gains' },
  { href: '/assistant', label: 'Assistant' },
];

export default function PropertiesPage() {
  const router = useRouter();
  const [user, setUser]             = useState<User | null>(null);
  const [entities, setEntities]     = useState<Entity[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
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
        if (u.role === 'admin') {
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
    const qs = selectedId !== null ? `&entity_id=${selectedId}` : '';
    fetch(`${API_URL}/api/v1/manual-assets?category=properties${qs}`, { credentials: 'include', signal: controller.signal })
      .then(r => { if (r.status === 401) { router.push('/'); return null; } if (!r.ok) throw new Error('Failed to load properties.'); return r.json(); })
      .then((d: ManualAssetsResponse | null) => { if (d) setData(d); setLoading(false); didInitialLoad.current = true; })
      .catch(err => { if (err.name !== 'AbortError') { setError(err.message); setLoading(false); } });
    return () => controller.abort();
  }, [router, selectedId, retryCount]);

  const isAdmin       = user?.role === 'admin';
  const showEntity    = isAdmin && selectedId === null;
  const handleRetry   = useCallback(() => setRetryCount(c => c + 1), []);

  return (
    <main id="main-content" className="min-h-screen bg-page p-4 sm:p-8">
      <div className="max-w-screen-2xl mx-auto">
        <div className="flex flex-wrap justify-between items-start gap-3 mb-6">
          <div>
            <h1 className="text-2xl sm:text-3xl font-bold text-ink">Properties</h1>
            <span className="text-sm text-ghost">Real estate — valuations with deeds, plans and related documents</span>
          </div>
          <nav className="flex gap-1.5 flex-wrap" aria-label="Sections">
            {NAV.map(({ href, label }) => (
              <a key={href} href={href} aria-current={href === '/properties' ? 'page' : undefined}
                 className={`px-3 py-1.5 rounded text-xs font-medium transition-colors ${
                   href === '/properties' ? 'bg-prime text-prime-fg' : 'bg-card border border-rule text-dim hover:border-dim hover:text-ink'}`}>
                {label}
              </a>
            ))}
          </nav>
        </div>

        {isAdmin && entities.length > 0 && (
          <EntitySwitcher entities={entities} selectedId={selectedId} onSelect={setSelectedId} />
        )}

        {loading && !data && (
          <div className="bg-card rounded-lg border border-rule px-5 py-16 text-center text-sm text-ghost">Loading…</div>
        )}

        {error && !data && (
          <div role="alert" className="bg-card rounded-lg border border-rule px-5 py-5 flex items-start justify-between gap-4">
            <div>
              <p className="text-sm font-medium text-dim">Could not load properties</p>
              <p className="text-xs text-ghost mt-1">{error}</p>
            </div>
            <button onClick={handleRetry} className="shrink-0 text-xs border border-wire text-dim px-3 py-1.5 rounded hover:border-dim hover:text-ink transition-colors">Retry</button>
          </div>
        )}

        {data && (
          <div className="fade-in">
            <div className="bg-card rounded-lg border border-rule px-5 sm:px-6 py-4 mb-6 flex flex-wrap gap-8 items-end">
              <div>
                <p className="text-[11px] uppercase tracking-wide text-ghost">Total Property Value</p>
                <p className="text-2xl font-bold text-ink tabular-nums">{fmtINR(data.total_value)}</p>
              </div>
              <div>
                <p className="text-[11px] uppercase tracking-wide text-ghost">Properties</p>
                <p className="text-base font-semibold text-ink tabular-nums">{data.count}</p>
              </div>
            </div>

            {data.count === 0 ? (
              <div className="bg-card rounded-lg border border-rule px-5 py-16 text-center text-sm text-ghost">
                No properties recorded yet.
              </div>
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                {data.assets.map(a => (
                  <PropertyCard key={`${a.entity_id}-${a.label}`} a={a} showEntity={showEntity} onOpen={setLightbox} />
                ))}
              </div>
            )}
          </div>
        )}

        <p className="text-center text-xs text-ghost mt-8">IWS Finserv &copy; {new Date().getFullYear()}</p>
      </div>

      {lightbox != null && (
        <div className="fixed inset-0 z-50 bg-ink/80 flex items-center justify-center p-6" onClick={() => setLightbox(null)}>
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={fileUrl(lightbox)} alt="Property" className="max-w-full max-h-full object-contain rounded shadow-2xl" />
        </div>
      )}
    </main>
  );
}
