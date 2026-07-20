'use client';
import NavTabs from '@/app/components/NavTabs';
import { useEffect, useState, useCallback, Fragment } from 'react';
import { useRouter } from 'next/navigation';
import DragScroll from '@/app/components/DragScroll';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

interface Entity { id: number; name: string; }
interface FxRates { [currency: string]: { rate: number; date: string }; }

interface ManualInputRow {
  id?: number;
  entity_id: number;
  category: string;
  label: string;
  cost: string;
  current_value: string;
  prev_week_value: string;
  currency: string;
  raw_amount: string;
  fx_rate: string;
  inception_date: string;
  notes: string;
  updated_at?: string;
  updated_by?: string;
  _dirty?: boolean;
}

interface SavedInput {
  id: number;
  entity_id: number;
  entity_name: string;
  category: string;
  label: string;
  cost: number | null;
  current_value: number | null;
  prev_week_value: number | null;
  currency: string;
  raw_amount: number | null;
  fx_rate: number | null;
  inception_date: string | null;
  notes: string | null;
  updated_at: string;
  updated_by: string | null;
}

const CATEGORIES: { value: string; label: string; group: string }[] = [
  { value: 'liquid_fund',    label: 'MF — Liquid Fund',          group: 'Fixed Income' },
  { value: 'debt_fund',      label: 'MF — Debt Fund',            group: 'Fixed Income' },
  { value: 'arbitrage_fund', label: 'MF — Arbitrage Fund',       group: 'Fixed Income' },
  { value: 'ppf',            label: 'PPF',                        group: 'Fixed Income' },
  { value: 'pms',            label: 'PMS',                        group: 'Equity' },
  { value: 'direct_equity',  label: 'Direct Equity (Aggregated)', group: 'Equity' },
  { value: 'aif',            label: 'AIF',                        group: 'Equity' },
  { value: 'overseas_fund',  label: 'Overseas Fund',              group: 'Alternates' },
  { value: 'overseas_equity',label: 'Foreign Equity',             group: 'Alternates' },
  { value: 'forex',          label: 'Forex / Foreign Cash',       group: 'Alternates' },
  { value: 'gold_etf',       label: 'Gold / Silver ETF',          group: 'Alternates' },
  { value: 'unlisted',       label: 'Unlisted Equity',            group: 'Alternates' },
  { value: 'startup',        label: 'Startup',                    group: 'Alternates' },
  { value: 'art',            label: 'Art (Paintings)',            group: 'Alternates' },
  { value: 'collectibles',   label: 'Collectibles',               group: 'Alternates' },
  // Real estate moved to the dedicated property register (/properties page).
  { value: 'funds_transit',  label: 'Funds in Transit',           group: 'Other' },
  { value: 'broker_balance', label: 'Broker Balance',             group: 'Other' },
  { value: 'bank',           label: 'Bank Balance',               group: 'Other' },
];

const CURRENCIES = ['INR', 'USD', 'GBP', 'AED', 'SGD', 'EUR', 'HKD'];

const FOREIGN_CATS = new Set(['overseas_fund', 'overseas_equity', 'forex']);

// Categories whose cost / current value are derived from funding rounds +
// corporate events (Phase 3) rather than hand-typed.
const UNLISTED_CATS = new Set(['unlisted', 'startup']);

function fmtINR(n: number): string {
  return (n < 0 ? '−₹' : '₹') + Math.round(Math.abs(n)).toLocaleString('en-IN');
}

function emptyRow(entity_id: number): ManualInputRow {
  return {
    entity_id, category: 'pms', label: '', cost: '', current_value: '',
    prev_week_value: '', currency: 'INR', raw_amount: '', fx_rate: '',
    inception_date: '', notes: '', _dirty: true,
  };
}

function savedToRow(s: SavedInput): ManualInputRow {
  return {
    id: s.id,
    entity_id: s.entity_id,
    category: s.category,
    label: s.label,
    cost:            s.cost            != null ? String(s.cost)            : '',
    current_value:   s.current_value   != null ? String(s.current_value)   : '',
    prev_week_value: s.prev_week_value != null ? String(s.prev_week_value) : '',
    currency: s.currency || 'INR',
    raw_amount: s.raw_amount != null ? String(s.raw_amount) : '',
    fx_rate:    s.fx_rate    != null ? String(s.fx_rate)    : '',
    inception_date: s.inception_date || '',
    notes: s.notes || '',
    updated_at: s.updated_at,
    updated_by: s.updated_by || undefined,
    _dirty: false,
  };
}

interface AttachmentMeta {
  id: number; kind: string; original_name: string | null; mime: string | null;
  size_bytes: number | null; has_thumb: boolean;
}

// Art paintings + collectibles share the attachment + detail editor.
const ART_LIKE = new Set(['art', 'collectibles']);
const DEFAULT_KINDS = [{ value: 'document', label: 'Document' }];
const ART_KINDS = [
  { value: 'art_image',                 label: 'Photo' },
  { value: 'authentication_certificate', label: 'Authentication certificate' },
  { value: 'bill',                      label: 'Bill / invoice' },
  { value: 'document',                  label: 'Other document' },
];
const KIND_OPTS: Record<string, { value: string; label: string }[]> = {
  art:          ART_KINDS,
  collectibles: ART_KINDS,
};
function defaultKind(category: string): string {
  if (ART_LIKE.has(category)) return 'art_image';
  return 'document';
}

// ── Unlisted / startup funding-rounds editor ─────────────────────────────────

interface RoundForm {
  round_name: string; round_date: string; round_valuation: string;
  price_per_share: string; shares: string; amount_invested: string; notes: string;
}
interface EventForm {
  // split: `ratio` is the share multiplier (2 = 2-for-1). bonus: `bonus_shares`
  // is the absolute number of new shares issued.
  event_type: 'split' | 'bonus'; event_date: string; ratio: string; bonus_shares: string; notes: string;
}
interface RoundsAggregate {
  cost: number; total_shares: number; current_price_per_share: number | null;
  current_value: number | null; pnl: number | null;
  eventPool: Record<number, number>;   // resulting share pool after each event (by index)
}

function emptyRoundForm(): RoundForm {
  return { round_name: '', round_date: '', round_valuation: '', price_per_share: '', shares: '', amount_invested: '', notes: '' };
}
function emptyEventForm(): EventForm {
  return { event_type: 'split', event_date: '', ratio: '2', bonus_shares: '', notes: '' };
}

// Client-side mirror of the backend _compute_unlisted, for live preview. Walks
// rounds + events in date order over a running share pool: a SPLIT multiplies
// the pool by its ratio; a BONUS adds its share count (price-per-share drops so
// total value is unchanged).
function computeRoundsLocal(rounds: RoundForm[], events: EventForm[]): RoundsAggregate {
  const DMIN = -Infinity, DMAX = Infinity;
  type Item = { d: number; kind: 0 | 1; i: number };
  const items: Item[] = [];
  rounds.forEach((r, i) => items.push({ d: r.round_date ? Date.parse(r.round_date) : DMIN, kind: 0, i }));
  events.forEach((e, i) => items.push({ d: e.event_date ? Date.parse(e.event_date) : DMAX, kind: 1, i }));
  items.sort((a, b) => a.d - b.d || a.kind - b.kind || a.i - b.i);

  let pool = 0, cost = 0, lastPrice: number | null = null, postFactor = 1;
  const eventPool: Record<number, number> = {};
  for (const it of items) {
    if (it.kind === 0) {
      const r = rounds[it.i];
      const sh = r.shares ? parseFloat(r.shares) : 0;
      const pps = r.price_per_share ? parseFloat(r.price_per_share) : NaN;
      let amt = r.amount_invested ? parseFloat(r.amount_invested) : NaN;
      if (isNaN(amt)) amt = isNaN(pps) ? 0 : pps * sh;
      cost += amt || 0; pool += sh;
      if (!isNaN(pps)) { lastPrice = pps; postFactor = 1; }
    } else {
      const e = events[it.i];
      let f = 1;
      if (e.event_type === 'bonus') {
        const c = e.bonus_shares ? parseFloat(e.bonus_shares) : 0;
        f = pool > 0 ? (pool + c) / pool : 1; pool += c;
      } else {
        const ra = e.ratio ? parseFloat(e.ratio) : 1;
        f = ra > 0 ? ra : 1; pool *= f;
      }
      postFactor *= f;
      eventPool[it.i] = pool;
    }
  }
  const curPps = lastPrice != null ? lastPrice / postFactor : null;
  const value = curPps != null ? pool * curPps : null;
  return {
    cost: Math.round(cost * 100) / 100,
    total_shares: Math.round(pool * 10000) / 10000,
    current_price_per_share: curPps != null ? Math.round(curPps * 1e6) / 1e6 : null,
    current_value: value != null ? Math.round(value * 100) / 100 : null,
    pnl: value != null ? Math.round((value - cost) * 100) / 100 : null,
    eventPool,
  };
}

function RoundsEditor({ entityId, category, label, onSaved }: {
  entityId: number; category: string; label: string; onSaved?: () => void;
}) {
  const [rounds, setRounds] = useState<RoundForm[]>([]);
  const [events, setEvents] = useState<EventForm[]>([]);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');

  const load = useCallback(async () => {
    if (!label.trim()) { setRounds([]); setEvents([]); return; }
    const q = new URLSearchParams({ entity_id: String(entityId), category, label });
    const r = await fetch(`${API_URL}/api/v1/unlisted-rounds?${q.toString()}`, { credentials: 'include' });
    if (!r.ok) { setRounds([]); setEvents([]); return; }
    const d = await r.json();
    setRounds((d.rounds || []).map((x: { round_name?: string; round_date?: string; round_valuation?: number; price_per_share?: number; shares?: number; amount_invested?: number; notes?: string }) => ({
      round_name: x.round_name || '', round_date: x.round_date || '',
      round_valuation: x.round_valuation != null ? String(x.round_valuation) : '',
      price_per_share: x.price_per_share != null ? String(x.price_per_share) : '',
      shares: x.shares != null ? String(x.shares) : '',
      amount_invested: x.amount_invested != null ? String(x.amount_invested) : '',
      notes: x.notes || '',
    })));
    setEvents((d.events || []).map((x: { event_type?: string; event_date?: string; factor?: number; bonus_shares?: number; notes?: string }) => {
      const type = (x.event_type === 'bonus' ? 'bonus' : 'split') as 'split' | 'bonus';
      return {
        event_type: type, event_date: x.event_date || '',
        ratio: type === 'split' ? (x.factor != null ? String(x.factor) : '2') : '2',
        bonus_shares: type === 'bonus' ? (x.bonus_shares != null ? String(x.bonus_shares) : '') : '',
        notes: x.notes || '',
      };
    }));
  }, [entityId, category, label]);

  useEffect(() => { load(); }, [load]);

  const agg = computeRoundsLocal(rounds, events);

  function setRound(i: number, field: keyof RoundForm, v: string) {
    setRounds(rs => rs.map((r, j) => j === i ? { ...r, [field]: v } : r));
  }
  function setEvent(i: number, field: keyof EventForm, v: string) {
    setEvents(es => es.map((e, j) => j === i ? { ...e, [field]: v } : e));
  }

  async function save() {
    setBusy(true); setMsg('');
    const payload = {
      entity_id: entityId, category, label,
      rounds: rounds.map(r => ({
        round_name: r.round_name || null,
        round_date: r.round_date || null,
        round_valuation: r.round_valuation ? parseFloat(r.round_valuation) : null,
        price_per_share: r.price_per_share ? parseFloat(r.price_per_share) : null,
        shares: r.shares ? parseFloat(r.shares) : null,
        amount_invested: r.amount_invested ? parseFloat(r.amount_invested) : null,
        notes: r.notes || null,
      })),
      events: events.map(e => ({
        event_type: e.event_type,
        event_date: e.event_date || null,
        factor: e.event_type === 'split' ? (e.ratio ? parseFloat(e.ratio) : null) : null,
        bonus_shares: e.event_type === 'bonus' ? (e.bonus_shares ? parseFloat(e.bonus_shares) : null) : null,
        ratio_text: e.event_type === 'split' ? `${e.ratio}:1` : `+${e.bonus_shares}`,
        notes: e.notes || null,
      })),
    };
    const r = await fetch(`${API_URL}/api/v1/unlisted-rounds`, {
      method: 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    setBusy(false);
    if (r.ok) { setMsg('Rounds saved'); onSaved?.(); }
    else { const d = await r.json().catch(() => ({})); setMsg(d.detail || 'Save failed'); }
  }

  const inp = 'px-2 py-1 rounded text-xs outline-none';
  const inpStyle = { background: 'var(--card)', border: '1px solid var(--wire)', color: 'var(--ink)' };

  return (
    <div className="flex flex-col gap-3 pb-1">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <span className="text-[11px] font-semibold" style={{ color: 'var(--dim)' }}>Funding rounds</span>
        <button onClick={() => setRounds(rs => [...rs, emptyRoundForm()])}
                className="px-2 py-0.5 rounded text-[11px]"
                style={{ background: 'var(--card)', border: '1px solid var(--rule)', color: 'var(--dim)' }}>
          + Add round
        </button>
      </div>

      {rounds.length > 0 && (
        <table className="text-[11px] border-collapse">
          <thead>
            <tr style={{ color: 'var(--ghost)' }}>
              {['Round', 'Date', 'Valuation (₹)', 'Price/share', 'Shares', 'Amount (₹)', 'Notes', ''].map(h => (
                <th key={h} className="px-1.5 py-1 text-left font-medium">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rounds.map((r, i) => (
              <tr key={i}>
                <td className="px-1"><input value={r.round_name} placeholder="Seed / Series A" onChange={e => setRound(i, 'round_name', e.target.value)} className={`w-28 ${inp}`} style={inpStyle} /></td>
                <td className="px-1"><input type="date" value={r.round_date} onChange={e => setRound(i, 'round_date', e.target.value)} className={`w-32 ${inp}`} style={inpStyle} /></td>
                <td className="px-1"><input type="number" value={r.round_valuation} placeholder="company val." onChange={e => setRound(i, 'round_valuation', e.target.value)} className={`w-28 ${inp} text-right`} style={inpStyle} /></td>
                <td className="px-1"><input type="number" value={r.price_per_share} placeholder="100" onChange={e => setRound(i, 'price_per_share', e.target.value)} className={`w-24 ${inp} text-right`} style={inpStyle} /></td>
                <td className="px-1"><input type="number" value={r.shares} placeholder="1000" onChange={e => setRound(i, 'shares', e.target.value)} className={`w-24 ${inp} text-right`} style={inpStyle} /></td>
                <td className="px-1"><input type="number" value={r.amount_invested} placeholder={r.price_per_share && r.shares ? String(Math.round(parseFloat(r.price_per_share) * parseFloat(r.shares))) : 'auto'} onChange={e => setRound(i, 'amount_invested', e.target.value)} className={`w-28 ${inp} text-right`} style={inpStyle} /></td>
                <td className="px-1"><input value={r.notes} placeholder="—" onChange={e => setRound(i, 'notes', e.target.value)} className={`w-28 ${inp}`} style={inpStyle} /></td>
                <td className="px-1"><button onClick={() => setRounds(rs => rs.filter((_, j) => j !== i))} className="text-[11px]" style={{ color: 'var(--peril)' }}>✕</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div className="flex items-center justify-between flex-wrap gap-2 mt-1">
        <span className="text-[11px] font-semibold" style={{ color: 'var(--dim)' }}>Splits &amp; bonuses</span>
        <button onClick={() => setEvents(es => [...es, emptyEventForm()])}
                className="px-2 py-0.5 rounded text-[11px]"
                style={{ background: 'var(--card)', border: '1px solid var(--rule)', color: 'var(--dim)' }}>
          + Add split / bonus
        </button>
      </div>

      {events.length > 0 && (
        <table className="text-[11px] border-collapse">
          <thead>
            <tr style={{ color: 'var(--ghost)' }}>
              {['Type', 'Date', 'Split ratio / Bonus shares', 'New total shares', 'Notes', ''].map(h => (
                <th key={h} className="px-1.5 py-1 text-left font-medium">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {events.map((e, i) => (
              <tr key={i}>
                <td className="px-1">
                  <select value={e.event_type} onChange={ev => setEvent(i, 'event_type', ev.target.value)} className={`w-20 ${inp}`} style={inpStyle}>
                    <option value="split">Split</option>
                    <option value="bonus">Bonus</option>
                  </select>
                </td>
                <td className="px-1"><input type="date" value={e.event_date} onChange={ev => setEvent(i, 'event_date', ev.target.value)} className={`w-32 ${inp}`} style={inpStyle} /></td>
                <td className="px-1">
                  {e.event_type === 'split' ? (
                    <span className="inline-flex items-center gap-1">
                      <input type="number" value={e.ratio} onChange={ev => setEvent(i, 'ratio', ev.target.value)} className={`w-14 ${inp} text-right`} style={inpStyle} placeholder="2" />
                      <span style={{ color: 'var(--ghost)' }} className="whitespace-nowrap">-for-1 (× shares)</span>
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-1">
                      <input type="number" value={e.bonus_shares} onChange={ev => setEvent(i, 'bonus_shares', ev.target.value)} className={`w-24 ${inp} text-right`} style={inpStyle} placeholder="e.g. 1000" />
                      <span style={{ color: 'var(--ghost)' }} className="whitespace-nowrap">bonus shares</span>
                    </span>
                  )}
                </td>
                <td className="px-1.5 tabular-nums" style={{ color: 'var(--ink)' }}>
                  {agg.eventPool[i] != null ? agg.eventPool[i].toLocaleString('en-IN', { maximumFractionDigits: 2 }) : '—'}
                </td>
                <td className="px-1"><input value={e.notes} placeholder="—" onChange={ev => setEvent(i, 'notes', ev.target.value)} className={`w-28 ${inp}`} style={inpStyle} /></td>
                <td className="px-1"><button onClick={() => setEvents(es => es.filter((_, j) => j !== i))} className="text-[11px]" style={{ color: 'var(--peril)' }}>✕</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {/* Derived aggregate */}
      <div className="flex flex-wrap gap-x-5 gap-y-1 mt-1 px-2 py-1.5 rounded"
           style={{ background: 'var(--card)', border: '1px solid var(--rule)' }}>
        <span className="text-[11px]" style={{ color: 'var(--ghost)' }}>Cost <b style={{ color: 'var(--ink)' }}>{fmtINR(agg.cost)}</b></span>
        <span className="text-[11px]" style={{ color: 'var(--ghost)' }}>Shares <b style={{ color: 'var(--ink)' }}>{agg.total_shares.toLocaleString('en-IN')}</b></span>
        <span className="text-[11px]" style={{ color: 'var(--ghost)' }}>Price/share <b style={{ color: 'var(--ink)' }}>{agg.current_price_per_share != null ? '₹' + agg.current_price_per_share.toLocaleString('en-IN') : '—'}</b></span>
        <span className="text-[11px]" style={{ color: 'var(--ghost)' }}>Current value <b style={{ color: 'var(--ink)' }}>{agg.current_value != null ? fmtINR(agg.current_value) : '—'}</b></span>
        {agg.pnl != null && (
          <span className="text-[11px]" style={{ color: 'var(--ghost)' }}>P&amp;L <b style={{ color: agg.pnl >= 0 ? 'var(--gain)' : 'var(--peril)' }}>{agg.pnl >= 0 ? '+' : ''}{fmtINR(agg.pnl)}</b></span>
        )}
      </div>

      <div className="flex items-center gap-2">
        <button onClick={save} disabled={busy || !label.trim()}
                className="px-3 py-1 rounded text-xs font-medium"
                style={{ background: 'var(--prime)', color: 'var(--prime-fg)', opacity: busy || !label.trim() ? 0.6 : 1 }}>
          {busy ? 'Saving…' : 'Save rounds'}
        </button>
        {msg && <span className="text-[11px]" style={{ color: 'var(--ghost)' }}>{msg}</span>}
      </div>
    </div>
  );
}

function AssetExtras({ entityId, category, label, onSaved }: { entityId: number; category: string; label: string; onSaved?: () => void }) {
  const [atts, setAtts] = useState<AttachmentMeta[]>([]);
  const [kind, setKind] = useState(defaultKind(category));
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');
  const [painterName, setPainterName] = useState('');
  const [painterAbout, setPainterAbout] = useState('');
  const [location, setLocation] = useState('');
  const [sellerName, setSellerName] = useState('');
  const [sellerAddress, setSellerAddress] = useState('');
  const artLike = ART_LIKE.has(category);

  const load = useCallback(async () => {
    if (!label.trim()) { setAtts([]); return; }
    const q = new URLSearchParams({ entity_id: String(entityId), category, label });
    const r = await fetch(`${API_URL}/api/v1/manual-attachments?${q.toString()}`, { credentials: 'include' });
    if (r.ok) setAtts(await r.json());
    if (ART_LIKE.has(category)) {
      const ar = await fetch(`${API_URL}/api/v1/manual-assets?category=${category}&entity_id=${entityId}`, { credentials: 'include' });
      if (ar.ok) {
        const d = await ar.json();
        const m = (d.assets || []).find((a: { label: string }) => a.label === label);
        if (m) {
          setPainterName(m.painter_name || ''); setPainterAbout(m.painter_about || '');
          setLocation(m.location || ''); setSellerName(m.seller_name || '');
          setSellerAddress(m.seller_address || '');
        }
      }
    }
  }, [entityId, category, label]);

  useEffect(() => { load(); }, [load]);

  async function onUpload(file: File) {
    setBusy(true); setMsg('');
    const fd = new FormData();
    fd.append('entity_id', String(entityId));
    fd.append('category', category);
    fd.append('label', label);
    fd.append('kind', kind);
    fd.append('file', file);
    const r = await fetch(`${API_URL}/api/v1/manual-attachments`, { method: 'POST', credentials: 'include', body: fd });
    setBusy(false);
    if (r.ok) { setMsg('Uploaded'); load(); }
    else { const d = await r.json().catch(() => ({})); setMsg(d.detail || 'Upload failed'); }
  }

  async function onDelete(id: number) {
    if (!confirm('Delete this file?')) return;
    const r = await fetch(`${API_URL}/api/v1/manual-attachments/${id}`, { method: 'DELETE', credentials: 'include' });
    if (r.ok) load();
  }

  async function saveDetail() {
    setBusy(true); setMsg('');
    const r = await fetch(`${API_URL}/api/v1/art-detail`, {
      method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        entity_id: entityId, label,
        painter_name: painterName, painter_about: painterAbout,
        location, seller_name: sellerName, seller_address: sellerAddress,
      }),
    });
    setBusy(false);
    setMsg(r.ok ? 'Details saved' : 'Save failed');
  }

  if (!label.trim()) {
    return (
      <div className="px-4 py-4 text-[11px]" style={{ color: 'var(--ghost)' }}>
        Enter a name in the “Label / Name” column first — then you can{' '}
        {category === 'art' ? 'upload photos and add the painter, location & seller details'
          : category === 'collectibles' ? 'upload photos and add location, seller & valuation details'
          : UNLISTED_CATS.has(category) ? 'add funding rounds, splits/bonuses and supporting documents'
          : 'upload supporting documents and files'} here.
      </div>
    );
  }

  return (
    <div className="px-4 py-4 flex flex-col gap-4" style={{ fontSize: 12 }}>
      {UNLISTED_CATS.has(category) && (
        <RoundsEditor entityId={entityId} category={category} label={label} onSaved={onSaved} />
      )}
      {artLike && (
        <div className="flex flex-col gap-3">
          {category === 'art' && (
            <div className="flex flex-wrap gap-3 items-start">
              <div className="flex flex-col gap-1">
                <label className="text-[11px]" style={{ color: 'var(--dim)' }}>Painter name</label>
                <input value={painterName} onChange={e => setPainterName(e.target.value)} placeholder="e.g. M.F. Husain"
                       className="w-56 px-2 py-1 rounded text-xs outline-none"
                       style={{ background: 'var(--card)', border: '1px solid var(--wire)', color: 'var(--ink)' }} />
              </div>
              <div className="flex flex-col gap-1 flex-1 min-w-[14rem]">
                <label className="text-[11px]" style={{ color: 'var(--dim)' }}>About the painter / art</label>
                <textarea value={painterAbout} onChange={e => setPainterAbout(e.target.value)} rows={2}
                          placeholder="Short note on the artist / provenance"
                          className="w-full px-2 py-1 rounded text-xs outline-none"
                          style={{ background: 'var(--card)', border: '1px solid var(--wire)', color: 'var(--ink)' }} />
              </div>
            </div>
          )}
          <div className="flex flex-wrap gap-3 items-start">
            <div className="flex flex-col gap-1">
              <label className="text-[11px]" style={{ color: 'var(--dim)' }}>Location (where kept)</label>
              <input value={location} onChange={e => setLocation(e.target.value)} placeholder="e.g. Goa residence"
                     className="w-56 px-2 py-1 rounded text-xs outline-none"
                     style={{ background: 'var(--card)', border: '1px solid var(--wire)', color: 'var(--ink)' }} />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px]" style={{ color: 'var(--dim)' }}>Seller name</label>
              <input value={sellerName} onChange={e => setSellerName(e.target.value)}
                     className="w-56 px-2 py-1 rounded text-xs outline-none"
                     style={{ background: 'var(--card)', border: '1px solid var(--wire)', color: 'var(--ink)' }} />
            </div>
            <div className="flex flex-col gap-1 flex-1 min-w-[14rem]">
              <label className="text-[11px]" style={{ color: 'var(--dim)' }}>Seller address</label>
              <input value={sellerAddress} onChange={e => setSellerAddress(e.target.value)}
                     className="w-full px-2 py-1 rounded text-xs outline-none"
                     style={{ background: 'var(--card)', border: '1px solid var(--wire)', color: 'var(--ink)' }} />
            </div>
            <button onClick={saveDetail} disabled={busy}
                    className="mt-5 px-3 py-1 rounded text-xs font-medium"
                    style={{ background: 'var(--prime)', color: 'var(--prime-fg)', opacity: busy ? 0.6 : 1 }}>
              Save details
            </button>
          </div>
          <p className="text-[11px]" style={{ color: 'var(--ghost)' }}>
            Purchase price uses the Cost column; current valuation uses Current Value.
          </p>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] font-medium" style={{ color: 'var(--dim)' }}>
          {artLike ? 'Photos / certificates / bills' : 'Documents / files'}:
        </span>
        <select value={kind} onChange={e => setKind(e.target.value)}
                className="px-2 py-1 rounded text-xs outline-none"
                style={{ background: 'var(--card)', border: '1px solid var(--wire)', color: 'var(--ink)' }}>
          {(KIND_OPTS[category] || DEFAULT_KINDS).map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
        <label className="px-3 py-1 rounded text-xs font-medium cursor-pointer"
               style={{ background: 'var(--card)', border: '1px solid var(--rule)', color: 'var(--dim)' }}>
          {busy ? 'Uploading…' : '+ Upload file'}
          <input type="file" hidden disabled={busy}
                 onChange={e => { const f = e.target.files?.[0]; if (f) onUpload(f); e.target.value = ''; }} />
        </label>
        {msg && <span className="text-[11px]" style={{ color: 'var(--ghost)' }}>{msg}</span>}
      </div>

      {atts.length > 0 && (
        <div className="flex flex-wrap gap-3">
          {atts.map(a => {
            const isImg = (a.mime || '').startsWith('image/');
            return (
              <div key={a.id} className="rounded overflow-hidden flex flex-col"
                   style={{ border: '1px solid var(--rule)', width: 120 }}>
                {isImg ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img src={`${API_URL}/api/v1/manual-attachments/${a.id}/thumb`} alt={a.original_name || ''}
                       className="w-full h-20 object-cover" />
                ) : (
                  <a href={`${API_URL}/api/v1/manual-attachments/${a.id}/file`} target="_blank" rel="noopener noreferrer"
                     className="w-full h-20 flex items-center justify-center text-xs"
                     style={{ background: 'var(--card)', color: 'var(--dim)' }}>📄 open</a>
                )}
                <div className="flex items-center justify-between gap-1 px-1.5 py-1" style={{ background: 'var(--card)' }}>
                  <span className="truncate text-[10px]" style={{ color: 'var(--ghost)' }} title={a.original_name || ''}>
                    {a.original_name || a.kind}
                  </span>
                  <button onClick={() => onDelete(a.id)} className="text-[11px]" style={{ color: 'var(--peril)' }}>✕</button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default function ManualDataPage() {
  const router = useRouter();
  const [openExtras, setOpenExtras] = useState<number | null>(null);
  const [entities, setEntities] = useState<Entity[]>([]);
  const [selectedEntity, setSelectedEntity] = useState<number | null>(null);
  const [fxRates, setFxRates] = useState<FxRates>({});
  const [rows, setRows] = useState<ManualInputRow[]>([]);
  // Last-saved snapshot, used to revert individual rows or discard all edits.
  const [savedSnapshot, setSavedSnapshot] = useState<ManualInputRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');

  // Re-auth modal
  const [showAuth, setShowAuth] = useState(false);
  const [password, setPassword] = useState('');
  const [authError, setAuthError] = useState('');

  const fetchEntities = useCallback(async () => {
    const res = await fetch(`${API_URL}/api/v1/entities`, { credentials: 'include' });
    if (res.status === 401) { router.push('/'); return; }
    const data = await res.json();
    setEntities(data.map((e: { id: number; name: string }) => e));
    if (data.length > 0) setSelectedEntity(data[0].id);
  }, [router]);

  const fetchFxRates = useCallback(async () => {
    const res = await fetch(`${API_URL}/api/v1/fx-rates`, { credentials: 'include' });
    if (res.ok) setFxRates(await res.json());
  }, []);

  const fetchInputs = useCallback(async (eid: number) => {
    setLoading(true);
    setError('');
    const res = await fetch(`${API_URL}/api/v1/manual-inputs?entity_id=${eid}`, { credentials: 'include' });
    if (res.ok) {
      const data: SavedInput[] = await res.json();
      const mapped = data.map(savedToRow);
      setRows(mapped);
      setSavedSnapshot(mapped.map(r => ({ ...r })));
    } else {
      setRows([]);
      setSavedSnapshot([]);
    }
    setLoading(false);
  }, []);

  // Manual data entry is admin (IWS) only — entity logins get read-only views
  // on the dedicated pages (Art, Properties, Gold/Silver, …), not this editor.
  useEffect(() => {
    fetch(`${API_URL}/api/v1/me`, { credentials: 'include' })
      .then(r => r.ok ? r.json() : null)
      .then((u: { role?: string } | null) => {
        if (!u) { router.push('/'); return; }
        if (u.role !== 'admin') { router.push('/dashboard'); return; }
      })
      .catch(() => router.push('/'));
  }, [router]);

  useEffect(() => {
    fetchEntities();
    fetchFxRates();
  }, [fetchEntities, fetchFxRates]);

  useEffect(() => {
    if (selectedEntity != null) fetchInputs(selectedEntity);
  }, [selectedEntity, fetchInputs]);

  function addRow() {
    if (selectedEntity == null) return;
    setRows(r => [...r, emptyRow(selectedEntity)]);
  }

  async function removeRow(idx: number) {
    const row = rows[idx];
    // Unsaved (new) row → just drop it from the table.
    if (row.id == null) {
      setRows(r => r.filter((_, i) => i !== idx));
      return;
    }
    // Saved entry → delete it (and its files) from the server. Versioned, so this
    // removes every version of this (entity, category, label).
    if (!confirm(`Delete "${row.label || 'this entry'}" and all its files? This cannot be undone.`)) return;
    const q = new URLSearchParams({
      entity_id: String(row.entity_id), category: row.category, label: row.label,
    });
    const res = await fetch(`${API_URL}/api/v1/manual-inputs?${q.toString()}`, {
      method: 'DELETE', credentials: 'include',
    });
    if (res.ok) {
      setRows(r => r.filter((_, i) => i !== idx));
      setSavedSnapshot(s => s.filter(r => r.id !== row.id));
      setSuccess('Entry deleted.');
      setTimeout(() => setSuccess(''), 3000);
    } else {
      setError('Could not delete the entry. Please try again.');
    }
  }

  // Undo edits on one row: restore the saved version, or drop it if it was new.
  function revertRow(idx: number) {
    const row = rows[idx];
    const saved = row.id != null ? savedSnapshot.find(s => s.id === row.id) : undefined;
    if (saved) {
      setRows(r => r.map((x, i) => i === idx ? { ...saved } : x));
    } else {
      setRows(r => r.filter((_, i) => i !== idx));
    }
  }

  // Discard ALL unsaved changes — restore the last-saved snapshot.
  function discardAll() {
    setRows(savedSnapshot.map(r => ({ ...r })));
    setError('');
    setSuccess('Changes discarded.');
    setTimeout(() => setSuccess(''), 3000);
  }

  function updateRow(idx: number, field: keyof ManualInputRow, value: string) {
    setRows(r => r.map((row, i) => i === idx ? { ...row, [field]: value, _dirty: true } : row));
  }

  function autoFillFx(idx: number, currency: string, rawStr: string) {
    const fx = fxRates[currency]?.rate;
    if (fx && rawStr) {
      const inr = parseFloat(rawStr) * fx;
      setRows(r => r.map((row, i) =>
        i === idx
          ? { ...row, currency, raw_amount: rawStr, fx_rate: String(fx), current_value: String(Math.round(inr)), _dirty: true }
          : row
      ));
    } else {
      updateRow(idx, 'currency', currency);
    }
  }

  const dirtyRows = rows.filter(r => r._dirty);

  function openSaveModal() {
    if (dirtyRows.length === 0) { setError('No changes to save.'); return; }
    // Validate: every row needs a name and at least one monetary value, so an
    // empty row (e.g. picking "Art" and entering nothing) is rejected with a clear
    // message instead of saving a blank/garbage entry.
    for (const r of dirtyRows) {
      const catLabel = CATEGORIES.find(c => c.value === r.category)?.label || r.category;
      if (!r.label.trim()) {
        setError(`Please enter a name/label for the ${catLabel} entry before saving.`);
        return;
      }
      if (!r.current_value.trim() && !r.cost.trim() && !r.raw_amount.trim()) {
        setError(`"${r.label}" has no value entered — add a cost or current value before saving.`);
        return;
      }
      // Cost / current value / prev week are stored in INR, always. A foreign
      // row with no raw amount means the native figure was typed straight into
      // those INR columns, which nothing downstream can detect — the portfolio
      // totals just silently under-count it by the exchange rate. Block it here.
      if (r.currency !== 'INR' && !r.raw_amount.trim()) {
        setError(
          `"${r.label}" is a ${r.currency} entry but has no foreign amount. ` +
          `Cost and current value must be in INR — put the ${r.currency} figure in the ` +
          `"Foreign amt" column and the INR value is worked out for you.`
        );
        return;
      }
      if (r.currency !== 'INR' && !r.fx_rate.trim()) {
        setError(`"${r.label}" has a ${r.currency} amount but no FX rate — enter the rate used to convert it to INR.`);
        return;
      }
    }
    setPassword('');
    setAuthError('');
    setError('');
    setShowAuth(true);
  }

  async function confirmSave() {
    if (!password) { setAuthError('Please enter your password.'); return; }
    setSaving(true);
    setAuthError('');

    const inputs = dirtyRows.map(r => ({
      entity_id:       r.entity_id,
      category:        r.category,
      label:           r.label,
      cost:            r.cost            ? parseFloat(r.cost)            : null,
      current_value:   r.current_value   ? parseFloat(r.current_value)   : null,
      prev_week_value: r.prev_week_value ? parseFloat(r.prev_week_value) : null,
      currency:        r.currency,
      raw_amount:      r.raw_amount      ? parseFloat(r.raw_amount)      : null,
      fx_rate:         r.fx_rate         ? parseFloat(r.fx_rate)         : null,
      inception_date:  r.inception_date  || null,
      notes:           r.notes           || null,
    }));

    const res = await fetch(`${API_URL}/api/v1/manual-inputs`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password, inputs }),
    });

    setSaving(false);

    if (res.ok) {
      setShowAuth(false);
      setSuccess(`Saved ${inputs.length} item(s) successfully.`);
      if (selectedEntity != null) fetchInputs(selectedEntity);
      setTimeout(() => setSuccess(''), 4000);
    } else if (res.status === 401) {
      setAuthError('Incorrect password. Please try again.');
    } else {
      const data = await res.json().catch(() => ({}));
      setAuthError(data.detail || 'Save failed. Please try again.');
    }
  }

  const grouped = CATEGORIES.reduce<Record<string, typeof CATEGORIES>>((acc, c) => {
    (acc[c.group] ||= []).push(c);
    return acc;
  }, {});

  const entityName = entities.find(e => e.id === selectedEntity)?.name || '';

  return (
    <div className="min-h-screen" style={{ background: 'var(--page)' }}>
      {/* Nav bar */}
      <header style={{ background: 'var(--card)', borderBottom: '1px solid var(--rule)' }}
              className="px-6 py-3 flex items-center justify-between">
        <div className="flex items-center gap-6 min-w-0 flex-1">
          <span className="font-bold text-sm" style={{ color: 'var(--ink)' }}>Rajani MIS</span>
          <NavTabs active="/manual-data" role={'admin'} variant="links" />
        </div>
      </header>

      <main id="main-content" className="max-w-7xl mx-auto px-4 py-6">
        <div className="flex items-center justify-between mb-5">
          <div>
            <h1 className="text-lg font-bold" style={{ color: 'var(--ink)' }}>Manual Data Entry</h1>
            <p className="text-xs mt-0.5" style={{ color: 'var(--ghost)' }}>
              PMS, bank balances, overseas assets, and other non-automated values
            </p>
          </div>
          <div className="flex gap-2">
            <button onClick={addRow}
                    className="px-3 py-1.5 rounded text-xs font-medium transition-colors"
                    style={{ background: 'var(--card)', border: '1px solid var(--rule)', color: 'var(--dim)' }}>
              + Add Row
            </button>
            {dirtyRows.length > 0 && (
              <button onClick={discardAll}
                      className="px-3 py-1.5 rounded text-xs font-medium transition-colors"
                      style={{ background: 'var(--card)', border: '1px solid var(--rule)', color: 'var(--peril)' }}>
                Discard Changes
              </button>
            )}
            <button onClick={openSaveModal}
                    className="px-4 py-1.5 rounded text-xs font-semibold transition-colors"
                    style={{ background: 'var(--prime)', color: 'var(--prime-fg)' }}>
              Save Changes {dirtyRows.length > 0 ? `(${dirtyRows.length})` : ''}
            </button>
          </div>
        </div>

        {/* Entity tabs */}
        <div className="flex flex-wrap gap-1.5 mb-5">
          {entities.map(e => (
            <button key={e.id} onClick={() => setSelectedEntity(e.id)}
                    className="px-3 py-1 rounded text-xs font-medium transition-colors"
                    style={e.id === selectedEntity
                      ? { background: 'var(--prime)', color: 'var(--prime-fg)' }
                      : { background: 'var(--card)', border: '1px solid var(--rule)', color: 'var(--dim)' }}>
              {e.name}
            </button>
          ))}
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

        {/* FX rates reference strip */}
        {Object.keys(fxRates).length > 0 && (
          <div className="mb-4 flex flex-wrap gap-3 px-3 py-2 rounded text-xs"
               style={{ background: 'var(--notice)', border: '1px solid var(--notice-edge)', color: 'var(--notice-dim)' }}>
            <span className="font-medium" style={{ color: 'var(--notice-ink)' }}>FX Rates (latest):</span>
            {Object.entries(fxRates).map(([cur, d]) => (
              <span key={cur}><span className="font-semibold" style={{ color: 'var(--notice-ink)' }}>1 {cur}</span> = ₹{d.rate.toFixed(2)} <span className="opacity-60">({d.date})</span></span>
            ))}
          </div>
        )}

        {loading ? (
          <div className="py-16 text-center text-xs" style={{ color: 'var(--ghost)' }}>Loading…</div>
        ) : (
          <div style={{ background: 'var(--card)', border: '1px solid var(--rule)', borderRadius: 8, overflow: 'hidden' }}>
            <DragScroll className="overflow-x-auto" role="region" aria-label="Manual data table" tabIndex={0}>
            <table className="w-full text-xs border-collapse" style={{ minWidth: 1100 }}>
              <thead>
                <tr style={{ background: 'var(--page)' }}>
                  {['Category', 'Label / Name', 'Cost (₹)', 'Current Value', 'Prev Week (₹)', 'Currency', 'Raw Amount', 'FX Rate', 'Inception Date', 'Notes', ''].map(h => (
                    <th key={h} className="px-3 py-2 text-left font-semibold whitespace-nowrap"
                        style={{ color: 'var(--dim)', borderBottom: '1px solid var(--rule)' }}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.length === 0 && (
                  <tr>
                    <td colSpan={11} className="px-4 py-10 text-center"
                        style={{ color: 'var(--ghost)' }}>
                      No entries for {entityName}. Click "+ Add Row" to add one.
                    </td>
                  </tr>
                )}
                {rows.map((row, idx) => {
                  const isForeign = FOREIGN_CATS.has(row.category) || row.currency !== 'INR';
                  const computedINR = row.raw_amount && row.fx_rate
                    ? (parseFloat(row.raw_amount) * parseFloat(row.fx_rate)).toFixed(0)
                    : null;
                  // Foreign row whose native figure was never captured — the INR
                  // columns are probably holding a foreign amount. Flagged inline
                  // so it is caught while typing, not at the save click.
                  const missingRaw = row.currency !== 'INR' && !row.raw_amount.trim();

                  const showFiles = true;   // attachments available for every manual entry
                  const isUnlisted = UNLISTED_CATS.has(row.category);
                  const isOpen = openExtras === idx;
                  return (
                    <Fragment key={idx}>
                    <tr
                        style={{
                          background: row._dirty ? 'oklch(97% 0.012 75)' : idx % 2 === 0 ? 'var(--card)' : 'var(--page)',
                          borderBottom: showFiles && isOpen ? 'none' : '1px solid var(--rule)',
                        }}>
                      {/* Category */}
                      <td className="px-2 py-1.5">
                        <select value={row.category}
                                onChange={e => updateRow(idx, 'category', e.target.value)}
                                className="w-40 px-2 py-1 rounded text-xs outline-none"
                                style={{ background: 'var(--page)', border: '1px solid var(--wire)', color: 'var(--ink)' }}>
                          {Object.entries(grouped).map(([grp, cats]) => (
                            <optgroup key={grp} label={grp}>
                              {cats.map(c => <option key={c.value} value={c.value}>{c.label}</option>)}
                            </optgroup>
                          ))}
                        </select>
                      </td>

                      {/* Label */}
                      <td className="px-2 py-1.5">
                        <input value={row.label} placeholder="e.g. Prudent Inv. Managers"
                               onChange={e => updateRow(idx, 'label', e.target.value)}
                               className="w-44 px-2 py-1 rounded text-xs outline-none"
                               style={{ background: 'var(--page)', border: '1px solid var(--wire)', color: 'var(--ink)' }} />
                      </td>

                      {/* Cost */}
                      <td className="px-2 py-1.5">
                        <input type="number" value={row.cost} placeholder="0"
                               onChange={e => updateRow(idx, 'cost', e.target.value)}
                               className="w-28 px-2 py-1 rounded text-xs outline-none text-right"
                               style={{ background: 'var(--page)', border: '1px solid var(--wire)', color: 'var(--ink)' }} />
                      </td>

                      {/* Current value */}
                      <td className="px-2 py-1.5">
                        <div>
                          <input type="number" value={row.current_value} placeholder="0"
                                 onChange={e => updateRow(idx, 'current_value', e.target.value)}
                                 className="w-28 px-2 py-1 rounded text-xs outline-none text-right"
                                 style={{ background: 'var(--page)', border: '1px solid var(--wire)', color: 'var(--ink)' }} />
                          {isUnlisted ? (
                            <div className="text-right mt-0.5" style={{ color: 'var(--ghost)', fontSize: 9 }}>or use 📊 Rounds</div>
                          ) : computedINR ? (
                            <div className="text-right mt-0.5" style={{ color: 'var(--ghost)', fontSize: 10 }}>
                              ≈ ₹{parseInt(computedINR).toLocaleString('en-IN')}
                            </div>
                          ) : missingRaw && (
                            <div className="text-right mt-0.5" style={{ color: 'var(--peril)', fontSize: 10 }}>
                              must be INR
                            </div>
                          )}
                        </div>
                      </td>

                      {/* Prev week */}
                      <td className="px-2 py-1.5">
                        <input type="number" value={row.prev_week_value} placeholder="0"
                               onChange={e => updateRow(idx, 'prev_week_value', e.target.value)}
                               className="w-28 px-2 py-1 rounded text-xs outline-none text-right"
                               style={{ background: 'var(--page)', border: '1px solid var(--wire)', color: 'var(--ink)' }} />
                      </td>

                      {/* Currency */}
                      <td className="px-2 py-1.5">
                        <select value={row.currency}
                                onChange={e => autoFillFx(idx, e.target.value, row.raw_amount)}
                                className="w-20 px-2 py-1 rounded text-xs outline-none"
                                style={{ background: 'var(--page)', border: '1px solid var(--wire)', color: 'var(--ink)' }}>
                          {CURRENCIES.map(c => <option key={c} value={c}>{c}</option>)}
                        </select>
                      </td>

                      {/* Raw amount (foreign) */}
                      <td className="px-2 py-1.5">
                        <input type="number" value={row.raw_amount}
                               placeholder={isForeign ? `${row.currency} amt` : '—'}
                               disabled={!isForeign}
                               title={missingRaw ? `Enter the ${row.currency} figure here — cost and current value are stored in INR.` : undefined}
                               onChange={e => autoFillFx(idx, row.currency, e.target.value)}
                               className="w-24 px-2 py-1 rounded text-xs outline-none text-right"
                               style={{
                                 background: isForeign ? 'var(--page)' : 'var(--rule)',
                                 border: `1px solid ${missingRaw ? 'var(--peril)' : 'var(--wire)'}`,
                                 color: 'var(--ink)',
                                 opacity: isForeign ? 1 : 0.4,
                               }} />
                      </td>

                      {/* FX rate */}
                      <td className="px-2 py-1.5">
                        <input type="number" value={row.fx_rate} placeholder={isForeign ? 'rate' : '—'}
                               disabled={!isForeign}
                               onChange={e => updateRow(idx, 'fx_rate', e.target.value)}
                               className="w-20 px-2 py-1 rounded text-xs outline-none text-right"
                               style={{
                                 background: isForeign ? 'var(--page)' : 'var(--rule)',
                                 border: '1px solid var(--wire)', color: 'var(--ink)',
                                 opacity: isForeign ? 1 : 0.4,
                               }} />
                      </td>

                      {/* Inception date */}
                      <td className="px-2 py-1.5">
                        <input type="date" value={row.inception_date}
                               onChange={e => updateRow(idx, 'inception_date', e.target.value)}
                               className="w-32 px-2 py-1 rounded text-xs outline-none"
                               style={{ background: 'var(--page)', border: '1px solid var(--wire)', color: 'var(--ink)' }} />
                      </td>

                      {/* Notes */}
                      <td className="px-2 py-1.5">
                        <input value={row.notes} placeholder="optional notes"
                               onChange={e => updateRow(idx, 'notes', e.target.value)}
                               className="w-36 px-2 py-1 rounded text-xs outline-none"
                               style={{ background: 'var(--page)', border: '1px solid var(--wire)', color: 'var(--ink)' }} />
                      </td>

                      {/* Files + Revert + Remove */}
                      <td className="px-2 py-1.5">
                        <div className="flex items-center gap-1">
                          {showFiles && (
                            <button onClick={() => setOpenExtras(isOpen ? null : idx)}
                                    title={ART_LIKE.has(row.category) ? 'Photos, details, certificates & bills'
                                      : isUnlisted ? 'Funding rounds, splits/bonuses & documents'
                                      : 'Attach documents / files'}
                                    className="px-2 py-0.5 rounded text-xs whitespace-nowrap"
                                    style={{ color: isOpen ? 'var(--prime-fg)' : 'var(--dim)', background: isOpen ? 'var(--prime)' : 'transparent', border: '1px solid var(--rule)' }}>
                              {ART_LIKE.has(row.category) ? '🖼 Photos' : isUnlisted ? '📊 Rounds' : '📎 Files'}
                            </button>
                          )}
                          {row._dirty && (
                            <button onClick={() => revertRow(idx)}
                                    title="Undo changes to this row"
                                    className="px-2 py-0.5 rounded text-xs"
                                    style={{ color: 'var(--dim)', background: 'transparent', border: '1px solid var(--rule)' }}>
                              ↶
                            </button>
                          )}
                          <button onClick={() => removeRow(idx)}
                                  title={row.id != null ? 'Delete this entry' : 'Remove row'}
                                  className="px-2 py-0.5 rounded text-xs"
                                  style={{ color: 'var(--peril)', background: 'transparent', border: '1px solid var(--rule)' }}>
                            ✕
                          </button>
                        </div>
                        {row.updated_by && (
                          <div className="text-center mt-0.5" style={{ color: 'var(--ghost)', fontSize: 9 }}>
                            {row.updated_by}
                          </div>
                        )}
                      </td>
                    </tr>
                    {showFiles && isOpen && (
                      <tr>
                        <td colSpan={11} style={{ background: 'var(--page)', borderBottom: '1px solid var(--rule)' }}>
                          <AssetExtras entityId={row.entity_id} category={row.category} label={row.label}
                                       onSaved={() => { if (selectedEntity != null) fetchInputs(selectedEntity); }} />
                        </td>
                      </tr>
                    )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
            </DragScroll>
          </div>
        )}
      </main>

      {/* Re-auth modal */}
      {showAuth && (
        <div className="fixed inset-0 z-50 flex items-center justify-center"
             style={{ background: 'rgba(0,0,0,0.5)' }}>
          <div className="rounded-xl p-6 w-full max-w-sm shadow-2xl"
               style={{ background: 'var(--card)', border: '1px solid var(--rule)' }}>
            <h2 className="text-sm font-bold mb-1" style={{ color: 'var(--ink)' }}>Confirm Save</h2>
            <p className="text-xs mb-4" style={{ color: 'var(--ghost)' }}>
              Re-enter your password to save {dirtyRows.length} change(s). This action will be logged.
            </p>

            <label className="block text-xs font-medium mb-1" style={{ color: 'var(--dim)' }}>Password</label>
            <input
              type="password"
              autoFocus
              value={password}
              onChange={e => setPassword(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && confirmSave()}
              placeholder="Your portal password"
              className="w-full px-3 py-2 rounded text-sm outline-none mb-1"
              style={{ background: 'var(--page)', border: `1px solid ${authError ? 'var(--peril)' : 'var(--wire)'}`, color: 'var(--ink)' }}
            />
            {authError && (
              <p className="text-xs mb-3" style={{ color: 'var(--peril)' }}>{authError}</p>
            )}

            <div className="flex gap-2 mt-4">
              <button onClick={() => setShowAuth(false)}
                      className="flex-1 py-2 rounded text-xs font-medium"
                      style={{ background: 'var(--page)', border: '1px solid var(--rule)', color: 'var(--dim)' }}>
                Cancel
              </button>
              <button onClick={confirmSave} disabled={saving}
                      className="flex-1 py-2 rounded text-xs font-semibold"
                      style={{ background: 'var(--prime)', color: 'var(--prime-fg)', opacity: saving ? 0.6 : 1 }}>
                {saving ? 'Saving…' : 'Confirm Save'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
