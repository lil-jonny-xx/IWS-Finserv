'use client';
// Private ornaments register — Jewellery / Gold / Silver.
//
// Modelled on the Collectibles gallery (card grid → photo modal → lightbox), but
// this one is editable in place: its owner is also the only person who maintains
// it, so there is no admin-only Manual Data detour. Every mutation is re-checked
// server-side by _require_ornaments_access — the redirect below is convenience,
// not the security boundary.
//
// Each piece carries a hand-entered valuation, which is authoritative. Where a
// metal weight and purity exist, the API also returns a spot-rate estimate; it
// is shown beside the valuation and never replaces it or the totals.
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useRouter } from 'next/navigation';
import { Glass } from '@/app/components/PrivacyGlass';
import PhotoLightbox from '@/app/components/PhotoLightbox';
import { ORNAMENTS_ENTITY_ID } from '@/app/lib/nav';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

type Category = 'jewellery' | 'gold' | 'silver';

interface Photo {
  id: number; original_name: string | null; mime: string | null;
  size_bytes: number | null; has_thumb: boolean; uploaded_at: string | null;
}

interface Ornament {
  id: number; category: Category; metal: string;
  serial_no: string | null; code: string | null;
  given_name: string | null; declared_name: string | null; item_type: string | null;
  gross_weight_g: number | null; metal_weight_g: number | null; purity: string | null;
  stones_carat: number | null; stones_note: string | null;
  quantity: number; mint: string | null; year_minted: number | null;
  assay_no: string | null; denomination: string | null; sealed: boolean | null;
  valuation: number | null; valuation_remark: string | null; valuation_date: string | null;
  purchased_from: string | null; invoice_no: string | null;
  purchase_date: string | null; purchase_price: number | null;
  notes: string | null; sort_order: number;
  fine_weight_g: number | null; spot_estimate: number | null;
  updated_at: string | null; photos: Photo[];
}

interface Totals {
  count: number; valuation: number; spot_estimate: number;
  gross_weight_g: number; metal_weight_g: number; stones_carat: number;
}

interface Spot { gold_per_g: number | null; silver_per_g: number | null; as_of: string | null }

interface RegisterResponse {
  entity_id: number; entity_name: string; is_owner: boolean;
  spot: Spot; items: Ornament[];
  totals: Record<string, Totals>;
}

const TABS: { key: Category; label: string; blurb: string }[] = [
  { key: 'jewellery', label: 'Jewellery', blurb: 'Worn pieces — weights, stones, valuation and provenance' },
  { key: 'gold',      label: 'Gold',      blurb: 'Coins, bars and articles — purity, mint and assay details' },
  { key: 'silver',    label: 'Silver',    blurb: 'Coins, bars, utensils and articles' },
];

// Every commonly worn form, grouped the way a jeweller's list reads — head down
// to feet, then the pieces that don't sit on the body.
const JEWELLERY_TYPES: { group: string; items: string[] }[] = [
  { group: 'Head & hair', items: ['Maang tikka', 'Matha patti', 'Passa / Jhoomar', 'Sheeshphool', 'Hair pin / Juda pin', 'Tiara'] },
  { group: 'Ears',        items: ['Earrings', 'Studs / Tops', 'Jhumka', 'Chandbali', 'Hoops / Bali', 'Ear cuff', 'Sahara / Kaan chain'] },
  { group: 'Nose',        items: ['Nath (nose ring)', 'Nose pin'] },
  { group: 'Neck',        items: ['Necklace', 'Necklace set', 'Choker', 'Chain', 'Pendant', 'Locket', 'Mangalsutra', 'Tanmaniya', 'Rani haar / Long haar', 'Collar'] },
  { group: 'Arms & hands',items: ['Bangle', 'Kada', 'Bracelet', 'Chooda / Chur', 'Armlet (Bajuband)', 'Ring', 'Engagement ring', 'Cocktail ring', 'Hathphool', 'Kalire'] },
  { group: 'Waist',       items: ['Kamarbandh / Vaddanam (waist belt)'] },
  { group: 'Feet',        items: ['Anklet (Payal)', 'Toe ring (Bichiya)'] },
  { group: 'Other',       items: ['Brooch', 'Cufflinks', 'Tie pin', 'Kurta buttons / Studs', 'Watch', 'Nazariya (baby)', 'Bridal / Full set', 'Loose stone', 'Other'] },
];

const BULLION_TYPES: Record<'gold' | 'silver', string[]> = {
  gold:   ['Coin', 'Sovereign coin', 'Bar / Biscuit', 'Ingot', 'Bullion round', 'Idol / Murti', 'Utensil / Article', 'Gift article', 'Granules / Scrap', 'Other'],
  silver: ['Coin', 'Bar / Biscuit', 'Ingot', 'Bullion round', 'Utensil / Article', 'Thali / Glass set', 'Cutlery / Serveware', 'Idol / Murti', 'Puja items', 'Payal / Anklet', 'Gift article', 'Granules / Scrap', 'Other'],
};

// Value is the millesimal fineness, which the API parses straight into a factor.
const PURITY_OPTS: Record<string, { value: string; label: string }[]> = {
  gold: [
    { value: '999.9', label: '24K — 999.9 fine' },
    { value: '999',   label: '24K — 999 fine' },
    { value: '958',   label: '23K — 958' },
    { value: '916',   label: '22K — 916' },
    { value: '875',   label: '21K — 875' },
    { value: '833',   label: '20K — 833' },
    { value: '750',   label: '18K — 750' },
    { value: '585',   label: '14K — 585' },
    { value: '375',   label: '9K — 375' },
  ],
  silver: [
    { value: '999', label: '999 — fine silver' },
    { value: '925', label: '925 — sterling' },
    { value: '916', label: '916' },
    { value: '900', label: '900 — coin silver' },
    { value: '800', label: '800' },
  ],
  platinum: [
    { value: '950', label: '950' },
    { value: '900', label: '900' },
    { value: '850', label: '850' },
  ],
  other: [],
};

function fmtMoney(n: number | null | undefined): string {
  if (n == null) return '—';
  return '₹' + Math.round(n).toLocaleString('en-IN');
}
function fmtWeight(n: number | null | undefined, unit = 'g'): string {
  if (n == null) return '—';
  return n.toLocaleString('en-IN', { maximumFractionDigits: 3 }) + ' ' + unit;
}
function photoFile(id: number)  { return `${API_URL}/api/v1/ornament-photos/${id}/file`; }
function photoThumb(id: number) { return `${API_URL}/api/v1/ornament-photos/${id}/thumb`; }

function displayName(o: Ornament): string {
  return o.given_name || o.declared_name || o.code || o.serial_no || `Item ${o.id}`;
}

// ── Card ────────────────────────────────────────────────────────────────────

function OrnamentCard({ o, onOpen }: { o: Ornament; onOpen: () => void }) {
  const cover = o.photos[0] || null;
  const sub = [o.item_type, o.purity ? `${o.purity} purity` : null,
               o.quantity > 1 ? `×${o.quantity}` : null].filter(Boolean).join(' · ');
  return (
    <button onClick={onOpen}
      className="text-left bg-card rounded-lg border border-rule overflow-hidden flex flex-col hover:border-dim transition-colors">
      <div className="w-full aspect-[4/3] bg-page overflow-hidden relative">
        {cover ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={cover.has_thumb ? photoThumb(cover.id) : photoFile(cover.id)}
               alt={displayName(o)} loading="lazy" className="w-full h-full object-cover" />
        ) : (
          <div className="w-full h-full flex items-center justify-center text-ghost text-xs">No image</div>
        )}
        {o.photos.length > 1 && (
          <span className="absolute bottom-1.5 right-1.5 text-[10px] bg-ink/60 text-white px-1.5 py-0.5 rounded">
            {o.photos.length} photos
          </span>
        )}
      </div>
      <div className="p-3.5 flex flex-col gap-1 flex-1">
        <div className="flex items-start justify-between gap-2">
          <h3 className="text-sm font-semibold text-ink leading-tight">{displayName(o)}</h3>
          <span className="text-sm font-bold text-ink tabular-nums shrink-0">{fmtMoney(o.valuation)}</span>
        </div>
        {sub && <p className="text-xs text-dim">{sub}</p>}
        <div className="flex items-center justify-between gap-2 mt-auto pt-1">
          <span className="text-[11px] text-ghost">
            {o.gross_weight_g != null ? `Gross ${fmtWeight(o.gross_weight_g)}` : (o.code || o.serial_no || '')}
          </span>
          {o.spot_estimate != null && (
            <span className="text-[11px] text-ghost tabular-nums" title="Indicative value at today's spot rate">
              spot {fmtMoney(o.spot_estimate)}
            </span>
          )}
        </div>
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

// ── Detail modal ────────────────────────────────────────────────────────────

function OrnamentModal({ o, onClose, onEdit, onDelete }: {
  o: Ornament; onClose: () => void; onEdit: () => void; onDelete: () => void;
}) {
  // Mounted under key={o.id} by the page, so the initial hero is always this
  // piece's first photo — no effect needed to resync when the item changes.
  const [heroId, setHeroId] = useState<number | null>(o.photos[0]?.id ?? null);
  const [zoom, setZoom] = useState<number | null>(null);
  const hero = o.photos.find(p => p.id === heroId) || o.photos[0] || null;
  const isJewellery = o.category === 'jewellery';

  return (
    <div className="fixed inset-0 z-50 bg-ink/70 flex items-start justify-center overflow-y-auto p-4 sm:p-8" onClick={onClose}>
      <div className="bg-card rounded-lg border border-rule w-full max-w-3xl" onClick={e => e.stopPropagation()}
           role="dialog" aria-label={displayName(o)}>
        <div className="p-4 sm:p-6">
          <div className="flex items-start justify-between gap-3 mb-3">
            <div>
              <h2 className="text-lg font-bold text-ink">{displayName(o)}</h2>
              {o.declared_name && o.declared_name !== o.given_name && (
                <p className="text-xs text-dim">Declared as {o.declared_name}</p>
              )}
              <p className="text-xs text-ghost">
                {[o.item_type, o.code ? `Code ${o.code}` : null, o.serial_no ? `S/N ${o.serial_no}` : null]
                  .filter(Boolean).join(' · ')}
              </p>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              <button onClick={onEdit}
                      className="text-xs px-2.5 py-1 rounded border border-rule text-dim hover:border-dim hover:text-ink transition-colors">
                Edit
              </button>
              <button onClick={onDelete} className="text-xs px-2.5 py-1 rounded border border-rule text-peril hover:border-peril transition-colors">
                Delete
              </button>
              <button onClick={onClose} aria-label="Close" className="text-ghost hover:text-ink text-xl leading-none">×</button>
            </div>
          </div>

          <div className="w-full aspect-[16/9] bg-page rounded-lg overflow-hidden mb-2 flex items-center justify-center">
            {hero ? (
              <button onClick={() => setZoom(hero.id)}
                      className="w-full h-full flex items-center justify-center cursor-zoom-in"
                      aria-label={`Zoom into photo of ${displayName(o)}`}>
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={photoFile(hero.id)} alt={displayName(o)} className="w-full h-full object-contain" />
              </button>
            ) : (
              <span className="text-ghost text-xs">No images uploaded</span>
            )}
          </div>
          {o.photos.length > 1 && (
            <div className="flex flex-wrap gap-2">
              {o.photos.map(p => (
                <button key={p.id} onClick={() => setHeroId(p.id)}
                        className={`w-16 h-16 rounded overflow-hidden border-2 ${p.id === heroId ? 'border-prime' : 'border-transparent'}`}>
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img src={p.has_thumb ? photoThumb(p.id) : photoFile(p.id)} alt="" className="w-full h-full object-cover" />
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="px-4 sm:px-6 pb-5 grid grid-cols-2 sm:grid-cols-3 gap-x-4 gap-y-3 border-t border-rule pt-4">
          <DetailRow label="Valuation">{fmtMoney(o.valuation)}</DetailRow>
          <DetailRow label="Valued on">{o.valuation_date || '—'}</DetailRow>
          <DetailRow label="Spot estimate">
            {o.spot_estimate != null ? fmtMoney(o.spot_estimate) : '—'}
            {o.fine_weight_g != null && (
              <span className="text-ghost"> ({fmtWeight(o.fine_weight_g)} fine)</span>
            )}
          </DetailRow>
          <DetailRow label="Gross weight">{fmtWeight(o.gross_weight_g)}</DetailRow>
          <DetailRow label={o.metal === 'silver' ? 'Silver weight' : 'Gold weight'}>
            {fmtWeight(o.metal_weight_g)}
          </DetailRow>
          <DetailRow label="Purity">{o.purity || '—'}</DetailRow>
          {isJewellery ? (
            <>
              <DetailRow label="Stones (carat)">{o.stones_carat != null ? `${o.stones_carat} ct` : '—'}</DetailRow>
              <DetailRow label="Stones">{o.stones_note || '—'}</DetailRow>
            </>
          ) : (
            <>
              <DetailRow label="Quantity">{o.quantity}</DetailRow>
              <DetailRow label="Mint / refiner">{o.mint || '—'}</DetailRow>
              <DetailRow label="Year">{o.year_minted ?? '—'}</DetailRow>
              <DetailRow label="Assay / certificate no.">{o.assay_no || '—'}</DetailRow>
              <DetailRow label="Denomination">{o.denomination || '—'}</DetailRow>
              <DetailRow label="Packaging">{o.sealed == null ? '—' : o.sealed ? 'Sealed / assay intact' : 'Opened'}</DetailRow>
            </>
          )}
          <DetailRow label="Purchased from">{o.purchased_from || '—'}</DetailRow>
          <DetailRow label="Invoice no.">{o.invoice_no || '—'}</DetailRow>
          <DetailRow label="Purchased on">{o.purchase_date || '—'}</DetailRow>
          <DetailRow label="Purchase price">{fmtMoney(o.purchase_price)}</DetailRow>
        </div>

        {(o.valuation_remark || o.notes) && (
          <div className="px-4 sm:px-6 pb-5">
            {o.valuation_remark && <>
              <p className="text-[10px] uppercase tracking-wide text-ghost">Valuation remark</p>
              <p className="text-xs text-dim whitespace-pre-wrap mb-2">{o.valuation_remark}</p>
            </>}
            {o.notes && <>
              <p className="text-[10px] uppercase tracking-wide text-ghost">Notes</p>
              <p className="text-xs text-dim whitespace-pre-wrap">{o.notes}</p>
            </>}
          </div>
        )}
      </div>

      {zoom != null && <PhotoLightbox src={photoFile(zoom)} alt={displayName(o)} onClose={() => setZoom(null)} />}
    </div>
  );
}

// ── Editor ──────────────────────────────────────────────────────────────────

// Every editable field is held as a string while the form is open — inputs give
// strings, and blank has to stay distinguishable from zero until save coerces.
// id and category keep their real types; photos are managed separately.
type DraftField = Exclude<keyof Ornament, 'id' | 'category' | 'photos'>;
type Draft = Partial<Record<DraftField, string>> & { id?: number; category: Category };

const INPUT_CLS = 'w-full px-2 py-1.5 rounded text-xs outline-none bg-card border border-wire text-ink focus:border-prime';

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[10px] uppercase tracking-wide text-ghost">{label}</span>
      {children}
      {hint && <span className="text-[10px] text-ghost">{hint}</span>}
    </label>
  );
}

function toDraft(o: Ornament | null, category: Category): Draft {
  if (!o) return { category, quantity: '1', metal: category === 'silver' ? 'silver' : 'gold' };
  const d: Draft = { id: o.id, category: o.category };
  const fields = d as Partial<Record<DraftField, string>>;
  (Object.keys(o) as (keyof Ornament)[]).forEach(k => {
    if (k === 'id' || k === 'category' || k === 'photos') return;
    const v = o[k];
    if (v == null) return;
    fields[k] = String(v);
  });
  return d;
}

function OrnamentEditor({ initial, category, onClose, onSaved }: {
  initial: Ornament | null; category: Category;
  onClose: () => void; onSaved: (saved: Ornament) => void;
}) {
  const [d, setD]         = useState<Draft>(() => toDraft(initial, category));
  const [photos, setPhotos] = useState<Photo[]>(initial?.photos ?? []);
  const [saving, setSaving] = useState(false);
  const [busy, setBusy]     = useState(false);
  const [err, setErr]       = useState('');
  const [msg, setMsg]       = useState('');
  const fileRef = useRef<HTMLInputElement>(null);

  const isJewellery = d.category === 'jewellery';
  const metal = d.metal || (d.category === 'silver' ? 'silver' : 'gold');
  const set = (k: string, v: string) => setD(prev => ({ ...prev, [k]: v }));

  const num = (v?: string) => {
    if (v == null || v.trim() === '') return null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  };
  const str = (v?: string) => (v == null || v.trim() === '' ? null : v.trim());

  async function save() {
    setSaving(true); setErr(''); setMsg('');
    const body = {
      id: d.id ?? null,
      category: d.category,
      metal,
      serial_no: str(d.serial_no), code: str(d.code),
      given_name: str(d.given_name), declared_name: str(d.declared_name),
      item_type: str(d.item_type),
      gross_weight_g: num(d.gross_weight_g), metal_weight_g: num(d.metal_weight_g),
      purity: str(d.purity),
      stones_carat: num(d.stones_carat), stones_note: str(d.stones_note),
      quantity: num(d.quantity) ?? 1,
      mint: str(d.mint), year_minted: num(d.year_minted),
      assay_no: str(d.assay_no), denomination: str(d.denomination),
      sealed: d.sealed === '' || d.sealed == null ? null : d.sealed === 'true',
      valuation: num(d.valuation), valuation_remark: str(d.valuation_remark),
      valuation_date: str(d.valuation_date),
      purchased_from: str(d.purchased_from), invoice_no: str(d.invoice_no),
      purchase_date: str(d.purchase_date), purchase_price: num(d.purchase_price),
      notes: str(d.notes), sort_order: num(d.sort_order) ?? 0,
    };
    const r = await fetch(`${API_URL}/api/v1/ornaments`, {
      method: 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    setSaving(false);
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      setErr(typeof e.detail === 'string' ? e.detail : 'Could not save.');
      return;
    }
    const saved: Ornament = await r.json();
    // Keep the form open on a first save so photos can be added right away —
    // uploads need the id the insert just produced.
    setD(prev => ({ ...prev, id: saved.id }));
    setPhotos(saved.photos);
    setMsg('Saved');
    onSaved(saved);
  }

  async function upload(file: File) {
    if (!d.id) { setErr('Save the piece first, then add photos.'); return; }
    setBusy(true); setErr(''); setMsg('');
    const fd = new FormData();
    fd.append('file', file);
    const r = await fetch(`${API_URL}/api/v1/ornaments/${d.id}/photos`, {
      method: 'POST', credentials: 'include', body: fd,
    });
    setBusy(false);
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      setErr(typeof e.detail === 'string' ? e.detail : 'Upload failed.');
      return;
    }
    const added: Photo = await r.json();
    setPhotos(prev => [...prev, added]);
    setMsg('Photo added');
  }

  async function removePhoto(id: number) {
    if (!confirm('Remove this photo?')) return;
    const r = await fetch(`${API_URL}/api/v1/ornament-photos/${id}`, {
      method: 'DELETE', credentials: 'include',
    });
    if (r.ok) setPhotos(prev => prev.filter(p => p.id !== id));
  }

  const typeOptions = isJewellery ? null : BULLION_TYPES[d.category as 'gold' | 'silver'];

  return (
    <div className="fixed inset-0 z-50 bg-ink/70 flex items-start justify-center overflow-y-auto p-4 sm:p-8" onClick={onClose}>
      <div className="bg-card rounded-lg border border-rule w-full max-w-3xl" onClick={e => e.stopPropagation()}
           role="dialog" aria-label={d.id ? 'Edit item' : 'Add item'}>
        <div className="flex items-start justify-between gap-3 p-4 sm:p-6 pb-3 border-b border-rule">
          <div>
            <h2 className="text-lg font-bold text-ink">{d.id ? 'Edit item' : 'Add item'}</h2>
            <p className="text-xs text-ghost">{TABS.find(t => t.key === d.category)?.label}</p>
          </div>
          <button onClick={onClose} aria-label="Close" className="text-ghost hover:text-ink text-xl leading-none">×</button>
        </div>

        <div className="p-4 sm:p-6 grid grid-cols-2 sm:grid-cols-3 gap-3">
          <Field label="Serial no.">
            <input className={INPUT_CLS} value={d.serial_no || ''} onChange={e => set('serial_no', e.target.value)} />
          </Field>
          <Field label="Code">
            <input className={INPUT_CLS} value={d.code || ''} onChange={e => set('code', e.target.value)} />
          </Field>
          <Field label="Quantity">
            <input className={INPUT_CLS} type="number" min={1} value={d.quantity || '1'}
                   onChange={e => set('quantity', e.target.value)} />
          </Field>
          <Field label="Name (yours)">
            <input className={INPUT_CLS} value={d.given_name || ''} onChange={e => set('given_name', e.target.value)}
                   placeholder="what you call it" />
          </Field>
          <Field label="Declared name">
            <input className={INPUT_CLS} value={d.declared_name || ''} onChange={e => set('declared_name', e.target.value)}
                   placeholder="as on paperwork" />
          </Field>
          <Field label="Category">
            {isJewellery ? (
              <select className={INPUT_CLS} value={d.item_type || ''} onChange={e => set('item_type', e.target.value)}>
                <option value="">—</option>
                {JEWELLERY_TYPES.map(g => (
                  <optgroup key={g.group} label={g.group}>
                    {g.items.map(i => <option key={i} value={i}>{i}</option>)}
                  </optgroup>
                ))}
              </select>
            ) : (
              <select className={INPUT_CLS} value={d.item_type || ''} onChange={e => set('item_type', e.target.value)}>
                <option value="">—</option>
                {typeOptions?.map(i => <option key={i} value={i}>{i}</option>)}
              </select>
            )}
          </Field>

          <Field label="Gross weight (g)">
            <input className={INPUT_CLS} type="number" step="0.001" value={d.gross_weight_g || ''}
                   onChange={e => set('gross_weight_g', e.target.value)} />
          </Field>
          <Field label={metal === 'silver' ? 'Silver weight (g)' : 'Gold weight (g)'}>
            <input className={INPUT_CLS} type="number" step="0.001" value={d.metal_weight_g || ''}
                   onChange={e => set('metal_weight_g', e.target.value)} />
          </Field>
          <Field label="Purity" hint="drives the spot estimate">
            <select className={INPUT_CLS} value={d.purity || ''} onChange={e => set('purity', e.target.value)}>
              <option value="">—</option>
              {(PURITY_OPTS[metal] || []).map(p => <option key={p.value} value={p.value}>{p.label}</option>)}
            </select>
          </Field>

          {isJewellery ? (
            <>
              <Field label="Stones (carat)" hint="precious + semi-precious, total">
                <input className={INPUT_CLS} type="number" step="0.001" value={d.stones_carat || ''}
                       onChange={e => set('stones_carat', e.target.value)} />
              </Field>
              <Field label="Stones — which">
                <input className={INPUT_CLS} value={d.stones_note || ''} onChange={e => set('stones_note', e.target.value)}
                       placeholder="e.g. 4 diamonds, 2 emeralds" />
              </Field>
              <Field label="Metal">
                <select className={INPUT_CLS} value={metal} onChange={e => set('metal', e.target.value)}>
                  <option value="gold">Gold</option>
                  <option value="silver">Silver</option>
                  <option value="platinum">Platinum</option>
                  <option value="other">Other</option>
                </select>
              </Field>
            </>
          ) : (
            <>
              <Field label="Mint / refiner">
                <input className={INPUT_CLS} value={d.mint || ''} onChange={e => set('mint', e.target.value)}
                       placeholder="e.g. MMTC-PAMP" />
              </Field>
              <Field label="Year minted">
                <input className={INPUT_CLS} type="number" value={d.year_minted || ''}
                       onChange={e => set('year_minted', e.target.value)} />
              </Field>
              <Field label="Assay / certificate no.">
                <input className={INPUT_CLS} value={d.assay_no || ''} onChange={e => set('assay_no', e.target.value)} />
              </Field>
              <Field label="Denomination">
                <input className={INPUT_CLS} value={d.denomination || ''} onChange={e => set('denomination', e.target.value)}
                       placeholder="face value, if any" />
              </Field>
              <Field label="Packaging">
                <select className={INPUT_CLS} value={d.sealed ?? ''} onChange={e => set('sealed', e.target.value)}>
                  <option value="">—</option>
                  <option value="true">Sealed / assay intact</option>
                  <option value="false">Opened</option>
                </select>
              </Field>
            </>
          )}

          <Field label="Valuation (₹)">
            <input className={INPUT_CLS} type="number" step="0.01" value={d.valuation || ''}
                   onChange={e => set('valuation', e.target.value)} />
          </Field>
          <Field label="Valued on">
            <input className={INPUT_CLS} type="date" value={d.valuation_date || ''}
                   onChange={e => set('valuation_date', e.target.value)} />
          </Field>
          <Field label="Purchased from">
            <input className={INPUT_CLS} value={d.purchased_from || ''} onChange={e => set('purchased_from', e.target.value)} />
          </Field>
          <Field label="Invoice no.">
            <input className={INPUT_CLS} value={d.invoice_no || ''} onChange={e => set('invoice_no', e.target.value)} />
          </Field>
          <Field label="Purchased on">
            <input className={INPUT_CLS} type="date" value={d.purchase_date || ''}
                   onChange={e => set('purchase_date', e.target.value)} />
          </Field>
          <Field label="Purchase price (₹)">
            <input className={INPUT_CLS} type="number" step="0.01" value={d.purchase_price || ''}
                   onChange={e => set('purchase_price', e.target.value)} />
          </Field>

          <div className="col-span-2 sm:col-span-3">
            <Field label="Valuation remark">
              <textarea className={INPUT_CLS} rows={2} value={d.valuation_remark || ''}
                        onChange={e => set('valuation_remark', e.target.value)}
                        placeholder="who valued it, on what basis" />
            </Field>
          </div>
          <div className="col-span-2 sm:col-span-3">
            <Field label="Notes">
              <textarea className={INPUT_CLS} rows={2} value={d.notes || ''} onChange={e => set('notes', e.target.value)} />
            </Field>
          </div>
        </div>

        <div className="px-4 sm:px-6 pb-4 border-t border-rule pt-4">
          <div className="flex flex-wrap items-center gap-2 mb-3">
            <span className="text-[10px] uppercase tracking-wide text-ghost">Photos</span>
            <button onClick={() => fileRef.current?.click()} disabled={busy || !d.id}
                    className="text-xs px-2.5 py-1 rounded border border-rule text-dim hover:border-dim hover:text-ink transition-colors disabled:opacity-50">
              {busy ? 'Uploading…' : '+ Add photo'}
            </button>
            <input ref={fileRef} type="file" accept="image/*" hidden
                   onChange={e => { const f = e.target.files?.[0]; if (f) upload(f); e.target.value = ''; }} />
            {!d.id && <span className="text-[11px] text-ghost">Save the piece first, then add photos.</span>}
          </div>
          {photos.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {photos.map(p => (
                <div key={p.id} className="rounded overflow-hidden border border-rule w-24">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img src={p.has_thumb ? photoThumb(p.id) : photoFile(p.id)} alt={p.original_name || ''}
                       className="w-full h-20 object-cover" />
                  <button onClick={() => removePhoto(p.id)}
                          className="w-full text-[10px] py-0.5 text-peril hover:underline">remove</button>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="px-4 sm:px-6 py-4 border-t border-rule flex items-center justify-end gap-3">
          {err && <span className="text-xs text-peril mr-auto">{err}</span>}
          {!err && msg && <span className="text-xs text-ghost mr-auto">{msg}</span>}
          <button onClick={onClose} className="text-xs px-3 py-1.5 rounded border border-rule text-dim hover:border-dim hover:text-ink transition-colors">
            Close
          </button>
          <button onClick={save} disabled={saving}
                  className="text-xs px-3 py-1.5 rounded bg-prime text-prime-fg font-medium disabled:opacity-60">
            {saving ? 'Saving…' : d.id ? 'Save changes' : 'Save item'}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Page ────────────────────────────────────────────────────────────────────

export default function OrnamentsPage() {
  const router = useRouter();
  const [allowed, setAllowed] = useState(false);
  const [tab, setTab]         = useState<Category>('jewellery');
  const [data, setData]       = useState<RegisterResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError]     = useState<string | null>(null);
  const [open, setOpen]       = useState<Ornament | null>(null);
  const [editing, setEditing] = useState<{ item: Ornament | null } | null>(null);

  // Convenience gate only — every endpoint re-checks server-side.
  useEffect(() => {
    fetch(`${API_URL}/api/v1/me`, { credentials: 'include' })
      .then(r => (r.ok ? r.json() : null))
      .then((u: { role?: string; entity_id?: number } | null) => {
        if (!u) { router.push('/'); return; }
        if (u.role !== 'admin' && u.entity_id !== ORNAMENTS_ENTITY_ID) { router.push('/dashboard'); return; }
        setAllowed(true);
      })
      .catch(() => router.push('/'));
  }, [router]);

  // Nothing is set before the first await, so calling this straight from an
  // effect doesn't cascade a render (react-hooks/set-state-in-effect).
  const load = useCallback(async () => {
    try {
      const r = await fetch(`${API_URL}/api/v1/ornaments`, { credentials: 'include' });
      if (r.status === 401) { router.push('/'); return; }
      if (r.status === 403) { router.push('/dashboard'); return; }
      if (!r.ok) throw new Error('Failed to load.');
      setData(await r.json());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load.');
    } finally {
      setLoading(false);
    }
  }, [router]);

  useEffect(() => { if (allowed) load(); }, [allowed, load]);

  const items = useMemo(
    () => (data?.items || []).filter(i => i.category === tab),
    [data, tab],
  );
  const totals = data?.totals?.[tab];

  async function remove(o: Ornament) {
    if (!confirm(`Delete “${displayName(o)}” and its photos? This cannot be undone.`)) return;
    const r = await fetch(`${API_URL}/api/v1/ornaments/${o.id}`, { method: 'DELETE', credentials: 'include' });
    if (r.ok) { setOpen(null); load(); }
  }

  if (!allowed) return null;

  const active = TABS.find(t => t.key === tab)!;
  const spot = data?.spot;

  return (
    <main id="main-content" className="min-h-screen bg-page py-4 sm:py-8">
      <div className="shell">
        <div className="flex flex-wrap justify-between items-start gap-3 mb-6">
          <div>
            <h1 className="text-2xl sm:text-3xl font-bold text-ink">Ornaments</h1>
            <span className="text-sm text-ghost">{active.blurb}</span>
          </div>
          <button onClick={() => setEditing({ item: null })}
                  className="text-xs px-3 py-1.5 rounded bg-prime text-prime-fg font-medium">
            + Add {active.label.toLowerCase()} item
          </button>
        </div>

        <div className="flex flex-wrap gap-1.5 mb-6">
          {TABS.map(t => (
            <button key={t.key} onClick={() => setTab(t.key)}
              className={`px-3 py-1.5 rounded text-xs font-medium uppercase tracking-wide transition-colors ${
                t.key === tab ? 'bg-prime text-prime-fg' : 'bg-card border border-rule text-dim hover:border-dim hover:text-ink'
              }`}>
              {t.label}
              {data?.totals?.[t.key] ? <span className="ml-1.5 opacity-70">{data.totals[t.key].count}</span> : null}
            </button>
          ))}
        </div>

        {loading && !data && (
          <div className="bg-card rounded-lg border border-rule px-5 py-16 text-center text-sm text-ghost">Loading…</div>
        )}

        {error && !data && (
          <div role="alert" className="bg-card rounded-lg border border-rule px-5 py-5 flex items-start justify-between gap-4">
            <div>
              <p className="text-sm font-medium text-dim">Could not load the register</p>
              <p className="text-xs text-ghost mt-1">{error}</p>
            </div>
            <button onClick={load} className="shrink-0 text-xs border border-wire text-dim px-3 py-1.5 rounded hover:border-dim hover:text-ink transition-colors">
              Retry
            </button>
          </div>
        )}

        {data && totals && (
          <div className="fade-in">
            <Glass className="mb-6">
              <div className="bg-card rounded-lg border border-rule px-5 sm:px-6 py-4 flex flex-wrap gap-8 items-end">
                <div>
                  <p className="text-[11px] uppercase tracking-wide text-ghost">Total Valuation</p>
                  <p className="text-2xl font-bold text-ink tabular-nums">{fmtMoney(totals.valuation)}</p>
                </div>
                <div>
                  <p className="text-[11px] uppercase tracking-wide text-ghost">Pieces</p>
                  <p className="text-base font-semibold text-ink tabular-nums">{totals.count}</p>
                </div>
                <div>
                  <p className="text-[11px] uppercase tracking-wide text-ghost">Gross weight</p>
                  <p className="text-base font-semibold text-ink tabular-nums">{fmtWeight(totals.gross_weight_g)}</p>
                </div>
                <div>
                  <p className="text-[11px] uppercase tracking-wide text-ghost">
                    {tab === 'silver' ? 'Silver weight' : 'Gold weight'}
                  </p>
                  <p className="text-base font-semibold text-ink tabular-nums">{fmtWeight(totals.metal_weight_g)}</p>
                </div>
                {tab === 'jewellery' && totals.stones_carat > 0 && (
                  <div>
                    <p className="text-[11px] uppercase tracking-wide text-ghost">Stones</p>
                    <p className="text-base font-semibold text-ink tabular-nums">{totals.stones_carat} ct</p>
                  </div>
                )}
                <div>
                  <p className="text-[11px] uppercase tracking-wide text-ghost">Spot estimate</p>
                  <p className="text-base font-semibold text-dim tabular-nums">{fmtMoney(totals.spot_estimate)}</p>
                </div>
              </div>
            </Glass>

            {spot && (spot.gold_per_g || spot.silver_per_g) && (
              <p className="text-[11px] text-ghost mb-4">
                Spot estimate is indicative only — metal weight × purity × today&apos;s spot rate
                {spot.gold_per_g ? ` (gold ₹${Math.round(spot.gold_per_g).toLocaleString('en-IN')}/g` : ''}
                {spot.silver_per_g ? `, silver ₹${Math.round(spot.silver_per_g).toLocaleString('en-IN')}/g` : ''}
                {spot.gold_per_g || spot.silver_per_g ? ')' : ''}
                {spot.as_of ? ` as on ${spot.as_of}` : ''}. It excludes stones and making charges.
                The entered valuation is the figure that counts.
              </p>
            )}

            {items.length === 0 ? (
              <div className="bg-card rounded-lg border border-rule px-5 py-16 text-center text-sm text-ghost">
                No {active.label.toLowerCase()} recorded yet.
              </div>
            ) : (
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
                {items.map(o => (
                  <OrnamentCard key={o.id} o={o} onOpen={() => setOpen(o)} />
                ))}
              </div>
            )}
          </div>
        )}

        <p className="text-center text-xs text-ghost mt-8">Rajani MIS &copy; {new Date().getFullYear()}</p>
      </div>

      {open && (
        <OrnamentModal
          key={open.id}
          o={open}
          onClose={() => setOpen(null)}
          onEdit={() => { setEditing({ item: open }); setOpen(null); }}
          onDelete={() => remove(open)}
        />
      )}

      {editing && (
        <OrnamentEditor
          initial={editing.item}
          category={editing.item?.category ?? tab}
          onClose={() => { setEditing(null); load(); }}
          onSaved={() => load()}
        />
      )}
    </main>
  );
}
