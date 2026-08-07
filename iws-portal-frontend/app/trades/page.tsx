'use client';
import { useEffect, useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

interface User { role: string; full_name: string; entity_id?: number; }
interface Entity { id: number; name: string; }

interface ManualTrade {
  id: number; entity_id: number; entity_name: string; broker: string;
  symbol: string; isin: string | null;
  transaction_date: string; transaction_type: 'BUY' | 'SELL';
  quantity: number; price: number; amount: number | null;
  notes: string | null; created_at: string;
}

// API-fed brokers sync holdings automatically — a manual entry here only fills gaps
// their feed can't see (demerger/bonus allotments, pre-history transfers) and adjusts
// the existing holding's cost basis. The non-API dematerialised brokers below have no
// feed, so a manual position at one is materialised as its own live-priced Equity row
// (see manual_positions_worker) — kept in sync with main.py NON_API_BROKERS.
const BROKERS = [
  { value: 'zerodha',        label: 'Zerodha (API)' },
  { value: 'angel_one',      label: 'Angel One (API)' },
  { value: 'dhan',           label: 'Dhan (API)' },
  { value: 'sbi_securities', label: 'SBI Securities' },
  { value: 'hdfc_securities',label: 'HDFC Securities' },
  { value: 'icici_direct',   label: 'ICICI Direct' },
  { value: 'kotak',          label: 'Kotak Securities' },
  { value: 'motilal_oswal',  label: 'Motilal Oswal' },
  { value: 'other',          label: 'Other / Off-market' },
];

// kind drives BUY/SELL on the backend; shown as a badge in the table via the
// [kind] prefix the API stores in notes.
const KINDS = [
  { value: 'buy',          label: 'Buy' },
  { value: 'sell',         label: 'Sell' },
  { value: 'transfer_in',  label: 'Transfer in (demat)' },
  { value: 'transfer_out', label: 'Transfer out (demat)' },
  { value: 'demerger',     label: 'Demerger allotment' },
  { value: 'ipo',          label: 'IPO allotment' },
  { value: 'bonus',        label: 'Bonus issue' },
  { value: 'rights',       label: 'Rights issue' },
];

function fmtINR(n: number | null | undefined): string {
  if (n == null) return '—';
  return '₹' + Math.round(n).toLocaleString('en-IN');
}

function kindOf(t: ManualTrade): string {
  const m = t.notes?.match(/^\[(\w+)\]/);
  const k = m ? m[1] : (t.transaction_type === 'BUY' ? 'buy' : 'sell');
  return KINDS.find(x => x.value === k)?.label ?? k;
}

function noteBody(t: ManualTrade): string {
  return (t.notes ?? '').replace(/^\[\w+\]\s*/, '');
}

const emptyForm = {
  entity_id: '', broker: 'zerodha', symbol: '', isin: '', kind: 'buy',
  trade_date: '', quantity: '', price: '', notes: '',
};

// Brokers whose exports the importer can parse. Deliberately narrower than BROKERS
// above — 'other' is a manual-entry bucket with no file format behind it.
const IMPORT_BROKERS = [
  { value: 'zerodha',   label: 'Zerodha (tradebook CSV / Console XLSX)' },
  { value: 'angel_one', label: 'Angel One (Trades_History XLSX)' },
  { value: 'dhan',      label: 'Dhan (TRADE_HISTORY CSV)' },
  { value: 'vested',    label: 'Vested (Transactions XLSX)' },
];

interface ImportResult {
  entity_name: string; broker: string; filename: string; committed: boolean;
  inserted: number; skipped: number; dupes: number; superseded: number;
  last_trade_date: string | null;
}

interface CorporateAction {
  id: number; action_type: string; ex_date: string; ratio: number;
  old_isin: string | null; new_isin: string | null; source: string;
  evidence: string | null; security_name: string; isin: string | null;
  entities: string[];
}

export default function TradesPage() {
  const router = useRouter();
  const [user, setUser]         = useState<User | null>(null);
  const [entities, setEntities] = useState<Entity[]>([]);
  const [trades, setTrades]     = useState<ManualTrade[] | null>(null);
  const [filterId, setFilterId] = useState<number | null>(null);
  const [form, setForm]         = useState({ ...emptyForm });
  const [busy, setBusy]         = useState(false);
  const [msg, setMsg]           = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);
  const [actions, setActions]   = useState<CorporateAction[] | null>(null);
  const [impForm, setImpForm]   = useState({ entity_id: '', broker: 'zerodha' });
  const [impFile, setImpFile]   = useState<File | null>(null);
  const [impResult, setImpResult] = useState<ImportResult | null>(null);
  const [impBusy, setImpBusy]   = useState(false);

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

  const loadTrades = useCallback(() => {
    const qs = filterId !== null ? `?entity_id=${filterId}` : '';
    fetch(`${API_URL}/api/v1/manual-trades${qs}`, { credentials: 'include' })
      .then(r => {
        if (r.status === 401) { router.push('/'); return null; }
        if (!r.ok) throw new Error('Failed to load trades');
        return r.json();
      })
      .then(d => { if (d) setTrades(d.trades); })
      .catch(() => setMsg({ kind: 'err', text: 'Could not load the trade register.' }));
  }, [router, filterId]);

  useEffect(() => { if (user?.role === 'admin') loadTrades(); }, [user, loadTrades]);

  const loadActions = useCallback(() => {
    fetch(`${API_URL}/api/v1/corporate-actions`, { credentials: 'include' })
      .then(r => (r.ok ? r.json() : { actions: [] }))
      .then(d => setActions(d.actions ?? []))
      .catch(() => setActions([]));
  }, []);

  useEffect(() => { if (user?.role === 'admin') loadActions(); }, [user, loadActions]);

  // Upload runs preview-then-commit: the operator sees the new/duplicate split before
  // anything is written, because a silent double-import is the costly mistake here.
  const runImport = (commit: boolean) => {
    if (!impFile || !impForm.entity_id) {
      setMsg({ kind: 'err', text: 'Pick an entity and a file first.' });
      return;
    }
    setImpBusy(true); setMsg(null);
    const fd = new FormData();
    fd.append('entity_id', impForm.entity_id);
    fd.append('broker', impForm.broker);
    fd.append('kind', 'tradebook');
    fd.append('file', impFile);
    fetch(`${API_URL}/api/v1/trades/tradebook/${commit ? 'commit' : 'preview'}`, {
      method: 'POST', credentials: 'include', body: fd,
    })
      .then(async r => {
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(d.detail || 'Import failed.');
        setImpResult(d);
        if (commit) {
          setMsg({ kind: 'ok', text: `Imported ${d.inserted} trade(s) from ${d.filename}.` });
          loadTrades();
        }
      })
      .catch(err => setMsg({ kind: 'err', text: err.message }))
      .finally(() => setImpBusy(false));
  };

  const set = (k: keyof typeof emptyForm) =>
    (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>) =>
      setForm(f => ({ ...f, [k]: e.target.value }));

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    setMsg(null);
    if (!form.entity_id || !form.symbol.trim() || !form.trade_date || !form.quantity) {
      setMsg({ kind: 'err', text: 'Entity, symbol, date and quantity are required.' });
      return;
    }
    setBusy(true);
    fetch(`${API_URL}/api/v1/manual-trades`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        entity_id: Number(form.entity_id),
        broker: form.broker,
        symbol: form.symbol.trim().toUpperCase(),
        isin: form.isin.trim() || null,
        kind: form.kind,
        trade_date: form.trade_date,
        quantity: Number(form.quantity),
        price: form.price === '' ? 0 : Number(form.price),
        notes: form.notes.trim() || null,
      }),
    })
      .then(async r => {
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(d.detail || 'Failed to record the trade.');
        setMsg({ kind: 'ok', text: d.message || 'Trade recorded.' });
        setForm(f => ({ ...f, symbol: '', isin: '', quantity: '', price: '', notes: '' }));
        loadTrades();
      })
      .catch(err => setMsg({ kind: 'err', text: err.message }))
      .finally(() => setBusy(false));
  };

  const remove = (id: number) => {
    if (!window.confirm('Delete this manual trade? Metrics will drop it on the next refresh.')) return;
    fetch(`${API_URL}/api/v1/manual-trades/${id}`, { method: 'DELETE', credentials: 'include' })
      .then(r => { if (!r.ok) throw new Error('Delete failed'); loadTrades(); })
      .catch(err => setMsg({ kind: 'err', text: err.message }));
  };

  const inputCls = 'bg-page border border-rule rounded px-2.5 py-1.5 text-sm text-ink ' +
                   'focus:outline-none focus:border-dim w-full';
  const labelCls = 'text-[11px] uppercase tracking-wide text-ghost mb-1 block';

  return (
    <main id="main-content" className="min-h-screen bg-page py-4 sm:py-8">
      <div className="shell">
        <div className="flex flex-wrap justify-between items-start gap-3 mb-6">
          <div>
            <h1 className="text-2xl sm:text-3xl font-bold text-ink">Trade Register</h1>
            <span className="text-sm text-ghost">
              Our own tradebook — transfers, demergers, IPO/bonus allotments and trades not yet covered by a broker import
            </span>
          </div>
        </div>

        {user && user.role !== 'admin' && (
          <div className="bg-card rounded-lg border border-rule px-5 py-16 text-center text-sm text-ghost">
            The trade register is available to administrators only.
          </div>
        )}

        {user?.role === 'admin' && (
          <>
            <form onSubmit={submit} className="bg-card rounded-lg border border-rule px-5 sm:px-6 py-5 mb-6">
              <p className="text-sm font-semibold text-ink mb-1">Record a trade</p>
              <p className="text-xs text-ghost mb-4">
                Only enter what broker tradebooks / API feeds don&apos;t already cover — duplicating an imported
                trade breaks the position reconstruction for that stock. Corporate-action allotments
                (demerger / bonus) can use price 0 or the apportioned cost.
              </p>
              <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-9 gap-3 items-end">
                <div className="col-span-2 sm:col-span-1 lg:col-span-1">
                  <label className={labelCls}>Entity</label>
                  <select className={inputCls} value={form.entity_id} onChange={set('entity_id')}>
                    <option value="">—</option>
                    {entities.map(e => <option key={e.id} value={e.id}>{e.name}</option>)}
                  </select>
                </div>
                <div>
                  <label className={labelCls}>Broker</label>
                  <select className={inputCls} value={form.broker} onChange={set('broker')}>
                    {BROKERS.map(b => <option key={b.value} value={b.value}>{b.label}</option>)}
                  </select>
                </div>
                <div>
                  <label className={labelCls}>Type</label>
                  <select className={inputCls} value={form.kind} onChange={set('kind')}>
                    {KINDS.map(k => <option key={k.value} value={k.value}>{k.label}</option>)}
                  </select>
                </div>
                <div>
                  <label className={labelCls}>Symbol</label>
                  <input className={inputCls} value={form.symbol} onChange={set('symbol')} placeholder="J&KBANK" />
                </div>
                <div>
                  <label className={labelCls}>ISIN <span className="normal-case">(if new)</span></label>
                  <input className={inputCls} value={form.isin} onChange={set('isin')} placeholder="INE…" />
                </div>
                <div>
                  <label className={labelCls}>Date</label>
                  <input type="date" className={inputCls} value={form.trade_date} onChange={set('trade_date')} />
                </div>
                <div>
                  <label className={labelCls}>Quantity</label>
                  <input type="number" min="0" step="any" className={inputCls} value={form.quantity} onChange={set('quantity')} />
                </div>
                <div>
                  <label className={labelCls}>Price ₹</label>
                  <input type="number" min="0" step="any" className={inputCls} value={form.price} onChange={set('price')} placeholder="0" />
                </div>
                <div>
                  <button type="submit" disabled={busy}
                          className="w-full bg-prime text-prime-fg text-sm font-medium rounded px-3 py-1.5 disabled:opacity-50">
                    {busy ? 'Saving…' : 'Add'}
                  </button>
                </div>
              </div>
              <div className="mt-3">
                <label className={labelCls}>Notes</label>
                <input className={inputCls} value={form.notes} onChange={set('notes')}
                       placeholder="e.g. Vedanta demerger allotment — 1 share per VEDL held" />
              </div>
              {msg && (
                <p role="status" className={`mt-3 text-xs ${msg.kind === 'ok' ? 'text-prime' : 'text-red-500'}`}>
                  {msg.text}
                </p>
              )}
            </form>

            {/* ---------------- monthly tradebook / ledger import ---------------- */}
            <div className="bg-card rounded-lg border border-rule px-5 sm:px-6 py-5 mb-6">
              <p className="text-sm font-semibold text-ink mb-1">Import a tradebook</p>
              <p className="text-xs text-ghost mb-4">
                For the monthly broker export. Preview first — it reports how many rows are new
                versus already held, and writes nothing. Re-uploading an overlapping export is
                safe: every fill is keyed by broker, entity, date and trade&nbsp;id, so the
                overlap is skipped rather than double-counted.
              </p>
              <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3 items-end">
                <div>
                  <label className={labelCls}>Entity</label>
                  <select className={inputCls} value={impForm.entity_id}
                          onChange={e => setImpForm(f => ({ ...f, entity_id: e.target.value }))}>
                    <option value="">—</option>
                    {entities.map(e => <option key={e.id} value={e.id}>{e.name}</option>)}
                  </select>
                </div>
                <div className="col-span-2">
                  <label className={labelCls}>Broker / format</label>
                  <select className={inputCls} value={impForm.broker}
                          onChange={e => setImpForm(f => ({ ...f, broker: e.target.value }))}>
                    {IMPORT_BROKERS.map(b => <option key={b.value} value={b.value}>{b.label}</option>)}
                  </select>
                </div>
                <div className="col-span-2">
                  <label className={labelCls}>File (.csv / .xlsx, max 15 MB)</label>
                  <input type="file" accept=".csv,.xlsx,.xlsm,.xls"
                         onChange={e => { setImpFile(e.target.files?.[0] ?? null); setImpResult(null); }}
                         className="block w-full text-xs text-dim file:mr-3 file:py-1.5 file:px-3
                                    file:rounded file:border file:border-rule file:text-xs
                                    file:bg-page file:text-ink hover:file:border-prime" />
                </div>
              </div>
              <div className="flex items-center gap-3 mt-4">
                <button type="button" disabled={impBusy || !impFile} onClick={() => runImport(false)}
                        className="text-sm px-3 py-1.5 rounded border border-rule text-ink
                                   hover:border-prime disabled:opacity-40 transition-colors">
                  {impBusy ? 'Working…' : 'Preview'}
                </button>
                <button type="button" disabled={impBusy || !impResult} onClick={() => runImport(true)}
                        className="text-sm px-3 py-1.5 rounded bg-prime text-white
                                   disabled:opacity-40 transition-opacity">
                  Import
                </button>
                {impResult && (
                  <span className="text-xs text-dim">
                    <strong className="text-ink">{impResult.inserted.toLocaleString('en-IN')}</strong> new
                    {' · '}{impResult.dupes.toLocaleString('en-IN')} already held
                    {' · '}{impResult.skipped.toLocaleString('en-IN')} skipped
                    {impResult.superseded > 0 && <> · {impResult.superseded} synthetic row(s) replaced</>}
                    {impResult.last_trade_date && <> · latest {impResult.last_trade_date}</>}
                    {!impResult.committed && <em className="text-ghost"> — preview only</em>}
                  </span>
                )}
              </div>
            </div>

            {/* ---------------- corporate actions ---------------- */}
            <div className="bg-card rounded-lg border border-rule overflow-hidden mb-6">
              <div className="px-5 py-3 border-b border-rule">
                <p className="text-sm font-semibold text-ink">
                  Corporate actions{' '}
                  {actions ? <span className="font-normal text-ghost">({actions.length})</span> : null}
                </p>
                <p className="text-xs text-ghost mt-0.5">
                  Splits and bonus issues, each verified against market data before being recorded.
                  Bonus shares are credited by the depository, so they appear in no tradebook and no
                  broker feed — without these rows a later sale looks like selling stock that was
                  never bought.
                </p>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-[11px] uppercase tracking-wide text-ghost border-b border-rule">
                      <th className="px-5 py-2 font-medium">Ex-date</th>
                      <th className="px-3 py-2 font-medium">Stock</th>
                      <th className="px-3 py-2 font-medium">Action</th>
                      <th className="px-3 py-2 font-medium text-right">Ratio</th>
                      <th className="px-3 py-2 font-medium">Holders</th>
                      <th className="px-3 py-2 font-medium">ISIN change</th>
                      <th className="px-3 py-2 font-medium">Verified from</th>
                    </tr>
                  </thead>
                  <tbody>
                    {actions === null && (
                      <tr><td colSpan={7} className="px-5 py-10 text-center text-ghost text-sm">Loading…</td></tr>
                    )}
                    {actions !== null && actions.length === 0 && (
                      <tr><td colSpan={7} className="px-5 py-10 text-center text-ghost text-sm">
                        No corporate actions recorded yet.
                      </td></tr>
                    )}
                    {actions?.map(a => (
                      <tr key={a.id} className="border-b border-rule last:border-0">
                        <td className="px-5 py-2 whitespace-nowrap text-dim">{a.ex_date}</td>
                        <td className="px-3 py-2">
                          <span className="text-ink font-medium">{a.security_name}</span>
                          {a.isin && <span className="text-[11px] text-ghost ml-1.5">{a.isin}</span>}
                        </td>
                        <td className="px-3 py-2">
                          <span className={`text-xs px-1.5 py-0.5 rounded ${
                            a.action_type === 'bonus'
                              ? 'bg-prime/10 text-prime' : 'bg-amber-500/10 text-amber-600'
                          }`}>{a.action_type}</span>
                        </td>
                        <td className="px-3 py-2 text-right tabular-nums text-ink">
                          {Number(a.ratio).toLocaleString('en-IN', { maximumFractionDigits: 4 })}×
                        </td>
                        <td className="px-3 py-2 text-xs text-dim">{a.entities?.join(', ') || '—'}</td>
                        <td className="px-3 py-2 text-[11px] text-ghost whitespace-nowrap">
                          {a.old_isin && a.new_isin && a.old_isin !== a.new_isin
                            ? `${a.old_isin} → ${a.new_isin}` : '—'}
                        </td>
                        <td className="px-3 py-2 text-xs text-ghost max-w-[30ch] truncate"
                            title={a.evidence ?? ''}>{a.source}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            <div className="bg-card rounded-lg border border-rule overflow-hidden">
              <div className="px-5 py-3 border-b border-rule flex items-center justify-between gap-3">
                <p className="text-sm font-semibold text-ink">
                  Recorded trades {trades ? <span className="font-normal text-ghost">({trades.length})</span> : null}
                </p>
                <select className="bg-page border border-rule rounded px-2 py-1 text-xs text-dim"
                        value={filterId ?? ''} onChange={e => setFilterId(e.target.value === '' ? null : Number(e.target.value))}>
                  <option value="">All entities</option>
                  {entities.map(e => <option key={e.id} value={e.id}>{e.name}</option>)}
                </select>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-[11px] uppercase tracking-wide text-ghost border-b border-rule">
                      <th className="px-5 py-2 font-medium">Date</th>
                      <th className="px-3 py-2 font-medium">Entity</th>
                      <th className="px-3 py-2 font-medium">Broker</th>
                      <th className="px-3 py-2 font-medium">Stock</th>
                      <th className="px-3 py-2 font-medium">Type</th>
                      <th className="px-3 py-2 font-medium text-right">Qty</th>
                      <th className="px-3 py-2 font-medium text-right">Price</th>
                      <th className="px-3 py-2 font-medium text-right">Value</th>
                      <th className="px-3 py-2 font-medium">Notes</th>
                      <th className="px-3 py-2" />
                    </tr>
                  </thead>
                  <tbody>
                    {trades === null && (
                      <tr><td colSpan={10} className="px-5 py-10 text-center text-ghost text-sm">Loading…</td></tr>
                    )}
                    {trades !== null && trades.length === 0 && (
                      <tr><td colSpan={10} className="px-5 py-10 text-center text-ghost text-sm">
                        No manual trades recorded yet.
                      </td></tr>
                    )}
                    {trades?.map(t => (
                      <tr key={t.id} className="border-b border-rule last:border-0">
                        <td className="px-5 py-2 whitespace-nowrap text-dim">{t.transaction_date}</td>
                        <td className="px-3 py-2 text-dim">{t.entity_name}</td>
                        <td className="px-3 py-2 text-dim">{BROKERS.find(b => b.value === t.broker)?.label ?? t.broker}</td>
                        <td className="px-3 py-2">
                          <span className="text-ink font-medium">{t.symbol}</span>
                          {t.isin && <span className="text-[11px] text-ghost ml-1.5">{t.isin}</span>}
                        </td>
                        <td className="px-3 py-2">
                          <span className={`text-xs px-1.5 py-0.5 rounded ${
                            t.transaction_type === 'BUY' ? 'bg-prime/10 text-prime' : 'bg-red-500/10 text-red-500'
                          }`}>{kindOf(t)}</span>
                        </td>
                        <td className="px-3 py-2 text-right tabular-nums text-ink">{t.quantity.toLocaleString('en-IN')}</td>
                        <td className="px-3 py-2 text-right tabular-nums text-ink">{t.price ? fmtINR(t.price) : '—'}</td>
                        <td className="px-3 py-2 text-right tabular-nums text-ink">{fmtINR(t.amount)}</td>
                        <td className="px-3 py-2 text-xs text-ghost max-w-[26ch] truncate" title={noteBody(t)}>{noteBody(t) || '—'}</td>
                        <td className="px-3 py-2 text-right">
                          <button onClick={() => remove(t.id)}
                                  className="text-xs text-ghost hover:text-red-500 transition-colors">Delete</button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </>
        )}
      </div>
    </main>
  );
}
