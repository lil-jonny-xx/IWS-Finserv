'use client';
import { useEffect, useState, useCallback } from 'react';
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

export default function ManualDataPage() {
  const router = useRouter();
  const [entities, setEntities] = useState<Entity[]>([]);
  const [selectedEntity, setSelectedEntity] = useState<number | null>(null);
  const [fxRates, setFxRates] = useState<FxRates>({});
  const [rows, setRows] = useState<ManualInputRow[]>([]);
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
      setRows(data.map(savedToRow));
    } else {
      setRows([]);
    }
    setLoading(false);
  }, []);

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

  function removeRow(idx: number) {
    setRows(r => r.filter((_, i) => i !== idx));
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

                  return (
                    <tr key={idx}
                        style={{
                          background: row._dirty ? 'oklch(97% 0.012 75)' : idx % 2 === 0 ? 'var(--card)' : 'var(--page)',
                          borderBottom: '1px solid var(--rule)',
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

                      {/* Remove */}
                      <td className="px-2 py-1.5">
                        <button onClick={() => removeRow(idx)}
                                className="px-2 py-0.5 rounded text-xs"
                                style={{ color: 'var(--peril)', background: 'transparent', border: '1px solid var(--rule)' }}>
                          ✕
                        </button>
                        {row.updated_by && (
                          <div className="text-center mt-0.5" style={{ color: 'var(--ghost)', fontSize: 9 }}>
                            {row.updated_by}
                          </div>
                        )}
                      </td>
                    </tr>
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
