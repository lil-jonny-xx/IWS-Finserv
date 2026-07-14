'use client';
import { useEffect, useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import NavTabs from '@/app/components/NavTabs';

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

const BROKERS = [
  { value: 'zerodha',   label: 'Zerodha' },
  { value: 'angel_one', label: 'Angel One' },
  { value: 'dhan',      label: 'Dhan' },
  { value: 'other',     label: 'Other / Off-market' },
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

export default function TradesPage() {
  const router = useRouter();
  const [user, setUser]         = useState<User | null>(null);
  const [entities, setEntities] = useState<Entity[]>([]);
  const [trades, setTrades]     = useState<ManualTrade[] | null>(null);
  const [filterId, setFilterId] = useState<number | null>(null);
  const [form, setForm]         = useState({ ...emptyForm });
  const [busy, setBusy]         = useState(false);
  const [msg, setMsg]           = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);

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
    <main id="main-content" className="min-h-screen bg-page p-4 sm:p-8">
      <div className="max-w-screen-2xl mx-auto">
        <div className="flex flex-wrap justify-between items-start gap-3 mb-6">
          <div>
            <h1 className="text-2xl sm:text-3xl font-bold text-ink">Trade Register</h1>
            <span className="text-sm text-ghost">
              Our own tradebook — transfers, demergers, IPO/bonus allotments and trades not yet covered by a broker import
            </span>
          </div>
          <NavTabs active="/trades" role={user?.role} />
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
