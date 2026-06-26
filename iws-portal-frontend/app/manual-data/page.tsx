'use client';
import { useEffect, useState, useCallback, Fragment } from 'react';
import { useRouter } from 'next/navigation';

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
  { value: 'overseas_equity',label: 'Overseas Direct Equity',     group: 'Alternates' },
  { value: 'forex',          label: 'Forex / Foreign Cash',       group: 'Alternates' },
  { value: 'gold_etf',       label: 'Gold / Silver ETF',          group: 'Alternates' },
  { value: 'unlisted',       label: 'Unlisted Equity',            group: 'Alternates' },
  { value: 'startup',        label: 'Startup',                    group: 'Alternates' },
  { value: 'art',            label: 'Art / Collectibles',         group: 'Alternates' },
  { value: 'properties',     label: 'Real Estate / Property',     group: 'Real Estate' },
  { value: 'funds_transit',  label: 'Funds in Transit',           group: 'Other' },
  { value: 'broker_balance', label: 'Broker Balance',             group: 'Other' },
  { value: 'bank',           label: 'Bank Balance',               group: 'Other' },
];

const CURRENCIES = ['INR', 'USD', 'GBP', 'AED', 'SGD', 'EUR', 'HKD'];

const FOREIGN_CATS = new Set(['overseas_fund', 'overseas_equity', 'forex']);

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

const DEFAULT_KINDS = [{ value: 'document', label: 'Document' }];
const KIND_OPTS: Record<string, { value: string; label: string }[]> = {
  art: [
    { value: 'art_image', label: 'Artwork image' },
    { value: 'document',  label: 'Certificate / document' },
  ],
  properties: [
    { value: 'deed',     label: 'Deed' },
    { value: 'plan',     label: 'Plan' },
    { value: 'document', label: 'Document' },
  ],
};
function defaultKind(category: string): string {
  if (category === 'art') return 'art_image';
  if (category === 'properties') return 'deed';
  return 'document';
}

function AssetExtras({ entityId, category, label }: { entityId: number; category: string; label: string }) {
  const [atts, setAtts] = useState<AttachmentMeta[]>([]);
  const [kind, setKind] = useState(defaultKind(category));
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');
  const [painterName, setPainterName] = useState('');
  const [painterAbout, setPainterAbout] = useState('');

  const load = useCallback(async () => {
    if (!label.trim()) { setAtts([]); return; }
    const q = new URLSearchParams({ entity_id: String(entityId), category, label });
    const r = await fetch(`${API_URL}/api/v1/manual-attachments?${q.toString()}`, { credentials: 'include' });
    if (r.ok) setAtts(await r.json());
    if (category === 'art') {
      const ar = await fetch(`${API_URL}/api/v1/manual-assets?category=art&entity_id=${entityId}`, { credentials: 'include' });
      if (ar.ok) {
        const d = await ar.json();
        const m = (d.assets || []).find((a: { label: string }) => a.label === label);
        if (m) { setPainterName(m.painter_name || ''); setPainterAbout(m.painter_about || ''); }
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

  async function savePainter() {
    setBusy(true); setMsg('');
    const r = await fetch(`${API_URL}/api/v1/art-detail`, {
      method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entity_id: entityId, label, painter_name: painterName, painter_about: painterAbout }),
    });
    setBusy(false);
    setMsg(r.ok ? 'Painter details saved' : 'Save failed');
  }

  if (!label.trim()) {
    return (
      <div className="px-4 py-4 text-[11px]" style={{ color: 'var(--ghost)' }}>
        Enter a name in the “Label / Name” column first — then you can{' '}
        {category === 'art' ? 'upload artwork photos and add the painter’s details'
          : category === 'properties' ? 'upload deeds, plans and other documents'
          : 'upload supporting documents and files'} here.
      </div>
    );
  }

  return (
    <div className="px-4 py-4 flex flex-col gap-4" style={{ fontSize: 12 }}>
      {category === 'art' && (
        <div className="flex flex-wrap gap-3 items-start">
          <div className="flex flex-col gap-1">
            <label className="text-[11px]" style={{ color: 'var(--dim)' }}>Painter name</label>
            <input value={painterName} onChange={e => setPainterName(e.target.value)} placeholder="e.g. M.F. Husain"
                   className="w-56 px-2 py-1 rounded text-xs outline-none"
                   style={{ background: 'var(--card)', border: '1px solid var(--wire)', color: 'var(--ink)' }} />
          </div>
          <div className="flex flex-col gap-1 flex-1 min-w-[14rem]">
            <label className="text-[11px]" style={{ color: 'var(--dim)' }}>About the painter</label>
            <textarea value={painterAbout} onChange={e => setPainterAbout(e.target.value)} rows={2}
                      placeholder="Short note on the artist / provenance"
                      className="w-full px-2 py-1 rounded text-xs outline-none"
                      style={{ background: 'var(--card)', border: '1px solid var(--wire)', color: 'var(--ink)' }} />
          </div>
          <button onClick={savePainter} disabled={busy}
                  className="mt-5 px-3 py-1 rounded text-xs font-medium"
                  style={{ background: 'var(--prime)', color: 'var(--prime-fg)', opacity: busy ? 0.6 : 1 }}>
            Save painter
          </button>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] font-medium" style={{ color: 'var(--dim)' }}>
          {category === 'art' ? 'Artwork image / documents'
            : category === 'properties' ? 'Deeds, plans & documents'
            : 'Documents / files'}:
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
      setSuccess(`Saved ${dirtyRows.length} item(s) successfully.`);
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
        <div className="flex items-center gap-6">
          <span className="font-bold text-sm" style={{ color: 'var(--ink)' }}>IWS MIS</span>
          <nav className="flex gap-4">
            {[
              { href: '/dashboard',   label: 'Dashboard' },
              { href: '/mutual-funds',label: 'Mutual Funds' },
              { href: '/equity',      label: 'Equity' },
              { href: '/foreign-equity', label: 'Foreign Equity' },
              { href: '/gold-silver', label: 'Gold/Silver' },
              { href: '/art', label: 'Art' },
              { href: '/properties', label: 'Properties' },
              { href: '/bank-accounts', label: 'Banks' },
              { href: '/pms', label: 'PMS' },
              { href: '/manual-data', label: 'Manual Data', active: true },
              { href: '/reports',     label: 'Reports' },
              { href: '/benchmarks',  label: 'Benchmarks' },
              { href: '/realised-gains', label: 'Realised Gains' },
              { href: '/assistant',   label: 'Assistant' },
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
            <table className="w-full text-xs border-collapse">
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

                  const showFiles = true;   // attachments available for every manual entry
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
                          {computedINR && (
                            <div className="text-right mt-0.5" style={{ color: 'var(--ghost)', fontSize: 10 }}>
                              ≈ ₹{parseInt(computedINR).toLocaleString('en-IN')}
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
                        <input type="number" value={row.raw_amount} placeholder={isForeign ? 'foreign amt' : '—'}
                               disabled={!isForeign}
                               onChange={e => autoFillFx(idx, row.currency, e.target.value)}
                               className="w-24 px-2 py-1 rounded text-xs outline-none text-right"
                               style={{
                                 background: isForeign ? 'var(--page)' : 'var(--rule)',
                                 border: '1px solid var(--wire)', color: 'var(--ink)',
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
                                    title={row.category === 'art' ? 'Artwork photos & painter details'
                                      : row.category === 'properties' ? 'Deeds, plans & documents'
                                      : 'Attach documents / files'}
                                    className="px-2 py-0.5 rounded text-xs whitespace-nowrap"
                                    style={{ color: isOpen ? 'var(--prime-fg)' : 'var(--dim)', background: isOpen ? 'var(--prime)' : 'transparent', border: '1px solid var(--rule)' }}>
                              {row.category === 'art' ? '🖼 Photos' : '📎 Files'}
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
                          <AssetExtras entityId={row.entity_id} category={row.category} label={row.label} />
                        </td>
                      </tr>
                    )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
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
