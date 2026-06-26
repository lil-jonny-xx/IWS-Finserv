'use client';
import { useEffect, useState, useCallback, useRef, Fragment } from 'react';
import { useRouter } from 'next/navigation';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

interface User { role: string; full_name: string; entity_id?: number; }
interface Entity { id: number; name: string; }

interface Attachment {
  id: number; kind: string; original_name: string | null; mime: string | null;
  size_bytes: number | null; has_thumb: boolean;
}
interface Breakdown {
  round_name: string | null; round_date: string | null; round_valuation: number | null;
  price_per_share: number | null; shares: number; amount_invested: number; effective_shares: number;
  current_value: number | null; notes: string | null;
}
interface UEvent {
  event_type: string; event_date: string | null; factor: number | null;
  bonus_shares: number | null; ratio_text: string | null; notes: string | null;
}
interface Aggregate {
  cost: number; total_shares: number; current_price_per_share: number | null;
  current_value: number | null; pnl: number | null; breakdown: Breakdown[];
}
interface UnlistedAsset {
  entity_id: number; entity_name: string; label: string;
  cost: number | null; current_value: number | null; currency: string;
  inception_date: string | null; notes: string | null;
  attachments: Attachment[]; events: UEvent[]; aggregate: Aggregate | null;
  category: string;
}
interface ManualAssetsResponse {
  category: string; entity_id: number; total_value: number; count: number; assets: UnlistedAsset[];
}

function fmtINR(n: number | null | undefined): string {
  if (n == null) return '—';
  return (n < 0 ? '−₹' : '₹') + Math.round(Math.abs(n)).toLocaleString('en-IN');
}
function fmtNum(n: number | null | undefined, dp = 0): string {
  if (n == null) return '—';
  return n.toLocaleString('en-IN', { maximumFractionDigits: dp });
}
function fileUrl(id: number)  { return `${API_URL}/api/v1/manual-attachments/${id}/file`; }
function thumbUrl(id: number) { return `${API_URL}/api/v1/manual-attachments/${id}/thumb`; }

const CAT_LABEL: Record<string, string> = { unlisted: 'Unlisted', startup: 'Startup' };

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

// A company name held by more than one entity is ONE company; the per-entity
// holdings are aggregated and broken out on expand.
interface MergedCompany {
  label: string; category: string; holdings: UnlistedAsset[];
  value: number; cost: number; pnl: number; shares: number; entities: string[];
}

function groupByLabel(assets: UnlistedAsset[]): MergedCompany[] {
  const map = new Map<string, UnlistedAsset[]>();
  for (const a of assets) {
    const key = a.label.trim().toLowerCase();
    if (!map.has(key)) map.set(key, []);
    map.get(key)!.push(a);
  }
  return [...map.values()].map(hs => {
    const value  = hs.reduce((s, a) => s + (a.aggregate?.current_value ?? a.current_value ?? 0), 0);
    const cost   = hs.reduce((s, a) => s + (a.aggregate?.cost ?? a.cost ?? 0), 0);
    const shares = hs.reduce((s, a) => s + (a.aggregate?.total_shares ?? 0), 0);
    return {
      label: hs[0].label, category: hs[0].category, holdings: hs,
      value, cost, pnl: value - cost, shares,
      entities: [...new Set(hs.map(a => a.entity_name).filter(Boolean) as string[])],
    };
  }).sort((a, b) => b.value - a.value);
}

// Rounds / events / documents for one entity's holding of a company.
function HoldingDetail({ a, header }: { a: UnlistedAsset; header: string | null }) {
  const rounds = a.aggregate?.breakdown ?? [];
  const docs   = a.attachments;
  return (
    <div className="flex flex-col gap-3">
      {header && (
        <p className="text-[11px] font-semibold text-ink flex items-center gap-2">
          {header}
          <span className="text-ghost font-normal">{fmtINR(a.aggregate?.current_value ?? a.current_value)}</span>
        </p>
      )}
      {rounds.length > 0 && (
        <div className="overflow-x-auto">
          <p className="text-[11px] font-semibold text-dim mb-1.5">Funding rounds</p>
          <table className="w-full text-[11px]" style={{ minWidth: '680px' }}>
            <thead>
              <tr className="text-ghost">
                {['Round', 'Date', 'Valuation', 'Price/share', 'Shares', 'Now (adj.)', 'Invested', 'Value'].map((h, i) => (
                  <th key={h} className={`px-2 py-1 font-medium ${i < 2 ? 'text-left' : 'text-right'} ${i === 0 ? 'pl-0' : ''}`}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rounds.map((r, i) => (
                <tr key={i} className="border-t border-rule/50">
                  <td className="px-2 py-1.5 pl-0 text-ink">{r.round_name || '—'}</td>
                  <td className="px-2 py-1.5 text-dim whitespace-nowrap">{r.round_date || '—'}</td>
                  <td className="px-2 py-1.5 text-right tabular-nums text-dim">{r.round_valuation != null ? fmtINR(r.round_valuation) : '—'}</td>
                  <td className="px-2 py-1.5 text-right tabular-nums text-dim">{r.price_per_share != null ? '₹' + fmtNum(r.price_per_share, 2) : '—'}</td>
                  <td className="px-2 py-1.5 text-right tabular-nums text-dim">{fmtNum(r.shares, 2)}</td>
                  <td className="px-2 py-1.5 text-right tabular-nums text-dim">{fmtNum(r.effective_shares, 2)}</td>
                  <td className="px-2 py-1.5 text-right tabular-nums text-dim">{fmtINR(r.amount_invested)}</td>
                  <td className="px-2 py-1.5 text-right tabular-nums text-ink">{fmtINR(r.current_value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {a.events.length > 0 && (
        <div>
          <p className="text-[11px] font-semibold text-dim mb-1.5">Splits &amp; bonuses</p>
          <div className="flex flex-wrap gap-2">
            {a.events.map((e, i) => (
              <span key={i} className="text-[11px] px-2 py-0.5 rounded border border-rule text-dim">
                {e.event_type === 'bonus'
                  ? <>Bonus{e.bonus_shares != null ? <span className="text-ghost"> · +{fmtNum(e.bonus_shares, 2)} shares</span> : ''}</>
                  : <>Split{e.factor != null ? <span className="text-ghost"> · shares ×{(+e.factor).toLocaleString('en-IN', { maximumFractionDigits: 3 })}</span> : ''}</>}
                {e.event_date && <span className="text-ghost"> · {e.event_date}</span>}
              </span>
            ))}
          </div>
        </div>
      )}

      {docs.length > 0 && (
        <div className="flex flex-wrap gap-2 items-center">
          <span className="text-[11px] font-semibold text-dim">Documents:</span>
          {docs.map(d => {
            const isImg = (d.mime || '').startsWith('image/');
            return isImg ? (
              // eslint-disable-next-line @next/next/no-img-element
              <a key={d.id} href={fileUrl(d.id)} target="_blank" rel="noopener noreferrer">
                <img src={thumbUrl(d.id)} alt={d.original_name || ''} loading="lazy"
                     className="h-12 w-12 object-cover rounded border border-rule" />
              </a>
            ) : (
              <a key={d.id} href={fileUrl(d.id)} target="_blank" rel="noopener noreferrer"
                 className="text-[11px] px-2 py-0.5 rounded border border-rule text-dim hover:text-ink hover:border-dim">
                📄 {d.original_name || 'document'}
              </a>
            );
          })}
        </div>
      )}

      {a.notes && <p className="text-[11px] text-ghost">{a.notes}</p>}
    </div>
  );
}

function CompanyRow({ c, showEntity, expanded, onToggle }: {
  c: MergedCompany; showEntity: boolean; expanded: boolean; onToggle: () => void;
}) {
  const multiEntity = c.holdings.length > 1;
  const hasDetail = c.holdings.some(h => (h.aggregate?.breakdown?.length ?? 0) > 0 || h.events.length > 0 || h.attachments.length > 0);

  return (
    <div className="bg-card rounded-lg border border-rule overflow-hidden">
      <button onClick={onToggle} disabled={!hasDetail}
        className="w-full flex flex-wrap items-center gap-x-6 gap-y-2 px-4 sm:px-5 py-3.5 text-left hover:bg-page transition-colors disabled:cursor-default">
        <span className="inline-block w-4 text-ghost text-[10px] shrink-0"
              style={{ transform: expanded ? 'rotate(90deg)' : 'none', transition: 'transform .15s' }}>
          {hasDetail ? '▶' : ''}
        </span>
        <div className="flex-1 min-w-[10rem]">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-sm font-semibold text-ink leading-tight">{c.label}</h3>
            <span className="text-[9px] uppercase tracking-wide px-1.5 py-0.5 rounded border border-rule text-ghost shrink-0">{CAT_LABEL[c.category] ?? c.category}</span>
            {multiEntity && (
              <span className="text-[9px] uppercase tracking-wide px-1.5 py-0.5 rounded border border-rule text-ghost shrink-0">{c.holdings.length} entities</span>
            )}
          </div>
          {showEntity && <p className="text-[11px] text-ghost mt-0.5">{c.entities.join(' · ') || '—'}</p>}
        </div>
        <div className="text-right w-20">
          <p className="text-[10px] uppercase tracking-wide text-ghost">Shares</p>
          <p className="text-xs font-medium text-dim tabular-nums">{fmtNum(c.shares, 0)}</p>
        </div>
        <div className="text-right w-28">
          <p className="text-[10px] uppercase tracking-wide text-ghost">Cost</p>
          <p className="text-xs font-medium text-dim tabular-nums">{fmtINR(c.cost)}</p>
        </div>
        <div className="text-right w-28">
          <p className="text-[10px] uppercase tracking-wide text-ghost">Value</p>
          <p className="text-sm font-bold text-ink tabular-nums">{fmtINR(c.value)}</p>
        </div>
        <div className="text-right w-28">
          <p className="text-[10px] uppercase tracking-wide text-ghost">P&amp;L</p>
          <p className="text-xs font-semibold tabular-nums" style={{ color: c.pnl >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
            {c.pnl >= 0 ? '+' : ''}{fmtINR(c.pnl)}
          </p>
        </div>
      </button>

      {expanded && hasDetail && (
        <div className="border-t border-rule px-4 sm:px-5 py-4 bg-page/40 flex flex-col gap-5 fade-in">
          {c.holdings.map((h, i) => (
            <HoldingDetail key={`${h.entity_id}-${i}`} a={h} header={multiEntity ? (h.entity_name || `Entity ${h.entity_id}`) : null} />
          ))}
        </div>
      )}
    </div>
  );
}

const NAV = [
  { href: '/dashboard', label: 'Overview' },
  { href: '/mutual-funds', label: 'Mutual Funds' },
  { href: '/equity', label: 'Equity' },
  { href: '/foreign-equity', label: 'Foreign Equity' },
  { href: '/bank-accounts', label: 'Banks' },
  { href: '/pms', label: 'PMS' },
  { href: '/gold-silver', label: 'Commodities' },
  { href: '/unlisted', label: 'Unlisted' },
  { href: '/properties', label: 'Properties' },
  { href: '/art', label: 'Art' },
  { href: '/realised-gains', label: 'Realised Gains' },
  { href: '/manual-data', label: 'Manual Data' },
  { href: '/reports', label: 'Reports' },
  { href: '/assistant', label: 'Assistant' },
];

export default function UnlistedPage() {
  const router = useRouter();
  const [user, setUser]             = useState<User | null>(null);
  const [entities, setEntities]     = useState<Entity[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [assets, setAssets]         = useState<UnlistedAsset[] | null>(null);
  const [loading, setLoading]       = useState(true);
  const [error, setError]           = useState<string | null>(null);
  const [retryCount, setRetryCount] = useState(0);
  const [expanded, setExpanded]     = useState<Set<string>>(new Set());
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
    Promise.all(['unlisted', 'startup'].map(cat =>
      fetch(`${API_URL}/api/v1/manual-assets?category=${cat}${qs}`, { credentials: 'include', signal: controller.signal })
        .then(r => { if (r.status === 401) { router.push('/'); return null; } if (!r.ok) throw new Error('Failed to load unlisted holdings.'); return r.json(); })
        .then((d: ManualAssetsResponse | null) => (d?.assets ?? []).map(a => ({ ...a, category: cat })))
    ))
      .then(lists => {
        const merged = ([] as UnlistedAsset[]).concat(...lists)
          .sort((x, y) => (y.aggregate?.current_value ?? y.current_value ?? 0) - (x.aggregate?.current_value ?? x.current_value ?? 0));
        setAssets(merged); setLoading(false); didInitialLoad.current = true;
      })
      .catch(err => { if (err.name !== 'AbortError') { setError(err.message); setLoading(false); } });
    return () => controller.abort();
  }, [router, selectedId, retryCount]);

  const isAdmin     = user?.role === 'admin';
  const showEntity  = isAdmin && selectedId === null;
  const handleRetry = useCallback(() => setRetryCount(c => c + 1), []);

  function toggle(key: string) {
    setExpanded(prev => { const n = new Set(prev); if (n.has(key)) n.delete(key); else n.add(key); return n; });
  }

  const companies  = assets ? groupByLabel(assets) : [];
  const totalValue = (assets ?? []).reduce((s, a) => s + (a.aggregate?.current_value ?? a.current_value ?? 0), 0);
  const totalCost  = (assets ?? []).reduce((s, a) => s + (a.aggregate?.cost ?? a.cost ?? 0), 0);
  const totalPnl   = totalValue - totalCost;

  return (
    <main id="main-content" className="min-h-screen bg-page p-4 sm:p-8">
      <div className="max-w-screen-2xl mx-auto">
        <div className="flex flex-wrap justify-between items-start gap-3 mb-6">
          <div>
            <h1 className="text-2xl sm:text-3xl font-bold text-ink">Unlisted &amp; Startups</h1>
            <span className="text-sm text-ghost">Private holdings valued from funding rounds, splits &amp; bonuses</span>
          </div>
          <nav className="flex gap-1.5 flex-wrap" aria-label="Sections">
            {NAV.map(({ href, label }) => (
              <a key={href} href={href} aria-current={href === '/unlisted' ? 'page' : undefined}
                 className={`px-3 py-1.5 rounded text-xs font-medium transition-colors ${
                   href === '/unlisted' ? 'bg-prime text-prime-fg' : 'bg-card border border-rule text-dim hover:border-dim hover:text-ink'}`}>
                {label}
              </a>
            ))}
          </nav>
        </div>

        {isAdmin && entities.length > 0 && (
          <EntitySwitcher entities={entities} selectedId={selectedId} onSelect={setSelectedId} />
        )}

        {loading && !assets && (
          <div className="bg-card rounded-lg border border-rule px-5 py-16 text-center text-sm text-ghost">Loading…</div>
        )}

        {error && !assets && (
          <div role="alert" className="bg-card rounded-lg border border-rule px-5 py-5 flex items-start justify-between gap-4">
            <div>
              <p className="text-sm font-medium text-dim">Could not load unlisted holdings</p>
              <p className="text-xs text-ghost mt-1">{error}</p>
            </div>
            <button onClick={handleRetry} className="shrink-0 text-xs border border-wire text-dim px-3 py-1.5 rounded hover:border-dim hover:text-ink transition-colors">Retry</button>
          </div>
        )}

        {assets && (
          <div className="fade-in">
            <div className="bg-card rounded-lg border border-rule px-5 sm:px-6 py-4 mb-6 flex flex-wrap gap-8 items-end">
              <div>
                <p className="text-[11px] uppercase tracking-wide text-ghost">Total Value</p>
                <p className="text-2xl font-bold text-ink tabular-nums">{fmtINR(totalValue)}</p>
              </div>
              <div>
                <p className="text-[11px] uppercase tracking-wide text-ghost">Total Cost</p>
                <p className="text-base font-semibold text-dim tabular-nums">{fmtINR(totalCost)}</p>
              </div>
              <div>
                <p className="text-[11px] uppercase tracking-wide text-ghost">P&amp;L</p>
                <p className="text-base font-semibold tabular-nums" style={{ color: totalPnl >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
                  {totalPnl >= 0 ? '+' : ''}{fmtINR(totalPnl)}
                </p>
              </div>
              <div>
                <p className="text-[11px] uppercase tracking-wide text-ghost">Companies</p>
                <p className="text-base font-semibold text-ink tabular-nums">{companies.length}</p>
              </div>
            </div>

            {companies.length === 0 ? (
              <div className="bg-card rounded-lg border border-rule px-5 py-16 text-center text-sm text-ghost">
                No unlisted or startup holdings recorded yet.
              </div>
            ) : (
              <div className="flex flex-col gap-3">
                {companies.map(c => {
                  const key = c.label.trim().toLowerCase();
                  return (
                    <Fragment key={key}>
                      <CompanyRow c={c} showEntity={showEntity} expanded={expanded.has(key)} onToggle={() => toggle(key)} />
                    </Fragment>
                  );
                })}
              </div>
            )}
          </div>
        )}

        <p className="text-center text-xs text-ghost mt-8">IWS Finserv &copy; {new Date().getFullYear()}</p>
      </div>
    </main>
  );
}
